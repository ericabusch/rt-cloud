import os
import sys
import time
import json
import re
import glob
import argparse
import logging
import threading
import websocket
from pathlib import Path
# import project modules
# Add base project path (two directories up)
currPath = os.path.dirname(os.path.realpath(__file__))
rootPath = os.path.dirname(currPath)
sys.path.append(rootPath)
from rtCommon.errors import StateError
from rtCommon.fileWatcher import FileWatcher
from rtCommon.readDicom import readDicomFromFile, anonymizeDicom, writeDicomToBuffer
from rtCommon.utils import DebugLevels, findNewestFile, installLoggers
from rtCommon.webClientUtils import login, certFile, checkSSLCertAltName, makeSSLCertFile
from rtCommon.webClientUtils import encodeMessageData, decodeMessageData

defaultAllowedDirs = ['/tmp', '/data']
defaultAllowedTypes = ['.dcm', '.mat']


class WebSocketFileWatcher:
    ''' A server that watches for files on the scanner computer and replies to
        cloud service requests with the file data.
    '''
    fileWatcher = FileWatcher()
    allowedDirs = None
    allowedTypes = None
    serverAddr = None
    sessionCookie = None
    needLogin = True
    shouldExit = False
    validationError = None
    # Synchronizing across threads
    clientLock = threading.Lock()
    fileWatchLock = threading.Lock()

    @staticmethod
    def runFileWatcher(serverAddr, retryInterval=10,
                       allowedDirs=defaultAllowedDirs,
                       allowedTypes=defaultAllowedTypes,
                       username=None, password=None,
                       testMode=False):
        WebSocketFileWatcher.serverAddr = serverAddr
        WebSocketFileWatcher.allowedDirs = allowedDirs
        for i in range(len(allowedTypes)):
            if not allowedTypes[i].startswith('.'):
                allowedTypes[i] = '.' + allowedTypes[i]
        WebSocketFileWatcher.allowedTypes = allowedTypes
        # go into loop trying to do webSocket connection periodically
        WebSocketFileWatcher.shouldExit = False
        while not WebSocketFileWatcher.shouldExit:
            try:
                if WebSocketFileWatcher.needLogin or WebSocketFileWatcher.sessionCookie is None:
                    WebSocketFileWatcher.sessionCookie = login(serverAddr, username, password, testMode=testMode)
                wsAddr = os.path.join('wss://', serverAddr, 'wsData')
                if testMode:
                    print("Warning: using non-encrypted connection for test mode")
                    wsAddr = os.path.join('ws://', serverAddr, 'wsData')
                logging.log(DebugLevels.L6, "Trying connection: %s", wsAddr)
                ws = websocket.WebSocketApp(wsAddr,
                                            on_message=WebSocketFileWatcher.on_message,
                                            on_close=WebSocketFileWatcher.on_close,
                                            on_error=WebSocketFileWatcher.on_error,
                                            cookie="login="+WebSocketFileWatcher.sessionCookie)
                logging.log(logging.INFO, "Connected to: %s", wsAddr)
                print("Connected to: {}".format(wsAddr))
                ws.run_forever(sslopt={"ca_certs": certFile})
            except Exception as err:
                logging.log(logging.INFO, "WSFileWatcher Exception {}: {}".format(type(err).__name__, str(err)))
                print('sleep {}'.format(retryInterval))
                time.sleep(retryInterval)

    @staticmethod
    def stop():
        WebSocketFileWatcher.shouldExit = True

    @staticmethod
    def on_message(client, message):
        fileWatcher = WebSocketFileWatcher.fileWatcher
        response = {'status': 400, 'error': 'unhandled request'}
        try:
            request = json.loads(message)
            cmd = request.get('cmd')
            dir = request.get('dir')
            filename = request.get('filename')
            timeout = request.get('timeout', 0)
            compress = request.get('compress', False)
            textOnly = False
            logging.log(logging.INFO, "{}: {} {}".format(cmd, dir, filename))
            # Do Validation Checks
            if dir is None and filename is not None:
                dir, filename = os.path.split(filename)
            if filename is None:
                errStr = "{}: Missing filename param".format(cmd)
                return send_error_response(client, request, errStr)
            if dir is None:
                errStr = "{}: Missing dir param".format(cmd)
                return send_error_response(client, request, errStr)
            if cmd in ('watchFile', 'getFile', 'getNewestFile'):
                if not os.path.isabs(dir):
                    # make path relative to the watch dir
                    dir = os.path.join(fileWatcher.watchDir, dir)
            if cmd in ('putTextFile', 'dataLog'):
                textOnly = True
            if WebSocketFileWatcher.validateRequestedFile(dir, filename, textFileTypeOnly=textOnly) is False:
                errStr = '{}: Non-allowed dir or filetype {} {}'.format(cmd, dir, filename)
                return send_error_response(client, request, errStr)
            if cmd in ('putTextFile', 'putBinaryFile', 'dataLog'):
                if not os.path.exists(dir):
                    os.makedirs(dir)
            if not os.path.exists(dir):
                errStr = '{}: No such directory: {}'.format(cmd, dir)
                return send_error_response(client, request, errStr)
            # Now handle requests
            if cmd == 'initWatch':
                minFileSize = request.get('minFileSize')
                demoStep = request.get('demoStep')
                if minFileSize is None:
                    errStr = "InitWatch: Missing minFileSize param"
                    return send_error_response(client, request, errStr)
                WebSocketFileWatcher.fileWatchLock.acquire()
                try:
                    fileWatcher.initFileNotifier(dir, filename, minFileSize, demoStep)
                finally:
                    WebSocketFileWatcher.fileWatchLock.release()
                response = {'status': 200}
            elif cmd == 'watchFile':
                WebSocketFileWatcher.fileWatchLock.acquire()
                filename = os.path.join(dir, filename)
                try:
                    retVal = fileWatcher.waitForFile(filename, timeout=timeout)
                finally:
                    WebSocketFileWatcher.fileWatchLock.release()
                if retVal is None:
                    errStr = "WatchFile: 408 Timeout {}s: {}".format(timeout, filename)
                    response = {'status': 408, 'error': errStr}
                    logging.log(logging.WARNING, errStr)
                else:
                    response = readDataCreateResponse(filename, compress)
            elif cmd == 'getFile':
                filename = os.path.join(dir, filename)
                if not os.path.exists(filename):
                    errStr = "GetFile: File not found {}".format(filename)
                    return send_error_response(client, request, errStr)
                response = readDataCreateResponse(filename, compress)
            elif cmd == 'getNewestFile':
                resultFilename = findNewestFile(dir, filename)
                if resultFilename is None or not os.path.exists(resultFilename):
                    errStr = 'GetNewestFile: file not found: {}'.format(os.path.join(dir, filename))
                    return send_error_response(client, request, errStr)
                response = readDataCreateResponse(resultFilename, compress)
            elif cmd == 'listFiles':
                if not os.path.isabs(dir):
                    errStr = "listFiles must have an absolute path: {}".format(dir)
                    return send_error_response(client, request, errStr)
                filePattern = os.path.join(dir, filename)
                fileList = [x for x in glob.iglob(filePattern)]
                response = {'status': 200, 'filePattern': filePattern, 'data': fileList}
            elif cmd == 'putTextFile':
                text = request.get('text')
                if text is None:
                    errStr = 'PutTextFile: Missing text field'
                    return send_error_response(client, request, errStr)
                elif type(text) is not str:
                    errStr = "PutTextFile: Only text data allowed"
                    return send_error_response(client, request, errStr)
                fullPath = os.path.join(dir, filename)
                with open(fullPath, 'w') as volFile:
                    volFile.write(text)
                response = {'status': 200}
            elif cmd == 'putBinaryFile':
                data = decodeMessageData(request)
                if data is None:
                    errStr = 'Error not defined'
                    if 'error' in request:
                        errStr = request['error']
                    return send_error_response(client, request, errStr)
                fullPath = os.path.join(dir, filename)
                with open(fullPath, 'wb') as binFile:
                    binFile.write(data)
                response = {'status': 200}
            elif cmd == 'dataLog':
                logLine = request.get('logLine')
                if logLine is None:
                    errStr = 'DataLog: Missing logLine field'
                    return send_error_response(client, request, errStr)
                fullPath = os.path.join(dir, filename)
                with open(fullPath, 'a') as logFile:
                    logFile.write(logLine + '\n')
                response = {'status': 200}
            elif cmd == 'ping':
                response = {'status': 200}
            elif cmd == 'error':
                errorCode = request['status']
                if errorCode == 401:
                    WebSocketFileWatcher.needLogin = True
                    WebSocketFileWatcher.sessionCookie = None
                errStr = 'Error {}: {}'.format(errorCode, request['error'])
                logging.log(logging.ERROR, request['error'])
                return
            else:
                errStr = 'OnMessage: Unrecognized command {}'.format(cmd)
                response = {'status': 400, 'error': errStr}
                logging.log(logging.WARNING, errStr)
        except Exception as err:
            errStr = "OnMessage Exception: {}: {}".format(cmd, err)
            logging.log(logging.WARNING, errStr)
            response = {'status': 400, 'error': errStr}
            if cmd == 'error':
                sys.exit()
        send_response(client, request, response)
        return

    @staticmethod
    def on_close(client):
        logging.info('connection closed')

    @staticmethod
    def on_error(client, error):
        if type(error) is KeyboardInterrupt:
            WebSocketFileWatcher.shouldExit = True
        else:
            logging.log(logging.WARNING, "on_error: WSFileWatcher: {} {}".
                        format(type(error), str(error)))

    @staticmethod
    def validateRequestedFile(dir, file, textFileTypeOnly=False):
        # Restrict requests to certain directories and file types
        WebSocketFileWatcher.validationError = None
        if WebSocketFileWatcher.allowedDirs is None or WebSocketFileWatcher.allowedTypes is None:
            raise StateError('Allowed Directories or File Types is not set')
        if file is not None and file != '':
            fileDir, filename = os.path.split(file)
            fileExtension = Path(filename).suffix
            if textFileTypeOnly:
                if fileExtension != '.txt':
                    WebSocketFileWatcher.validationError = 'Only .txt files allowed'
                    return False
            elif fileExtension not in WebSocketFileWatcher.allowedTypes:
                WebSocketFileWatcher.validationError = 'Not an allowed file type'
                return False
            if fileDir is not None and fileDir != '':  # and os.path.isabs(fileDir):
                dirMatch = False
                for allowedDir in WebSocketFileWatcher.allowedDirs:
                    if fileDir.startswith(allowedDir):
                        dirMatch = True
                        break
                if dirMatch is False:
                    WebSocketFileWatcher.validationError = 'Not within an allowed directory'
                    return False
        if dir is not None and dir != '':
            for allowedDir in WebSocketFileWatcher.allowedDirs:
                if dir.startswith(allowedDir):
                    return True
            WebSocketFileWatcher.validationError = 'Not within an allowed directory'
            return False
        # default case
        return True


def send_response(client, request, response):
    # merge response into the request dictionary
    request.update(response)
    response = request
    WebSocketFileWatcher.clientLock.acquire()
    try:
        client.send(json.dumps(response))
    finally:
        WebSocketFileWatcher.clientLock.release()


def send_error_response(client, request, errStr):
    logging.log(logging.WARNING, errStr)
    response = {'status': 400, 'error': errStr}
    send_response(client, request, response)


def readDataCreateResponse(filename, compress=False):
    data = readFile(filename)
    response = {'status': 200, 'filename': filename}
    response = encodeMessageData(response, data, compress)
    return response


def readFile(filename):
    data = None
    fileExtension = Path(filename).suffix
    if fileExtension == '.dcm':
        # Anonymize Dicom files
        dicomImg = readDicomFromFile(filename)
        dicomImg = anonymizeDicom(dicomImg)
        data = writeDicomToBuffer(dicomImg)
    else:
        with open(filename, 'rb') as fp:
            data = fp.read()
    return data


if __name__ == "__main__":
    installLoggers(logging.INFO, logging.INFO, filename='logs/fileWatcher.log')
    # do arg parse for server to connect to
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', action="store", dest="server", default="localhost:8888",
                        help="Server Address with Port [server:port]")
    parser.add_argument('-i', action="store", dest="interval", type=int, default=5,
                        help="Retry connection interval (seconds)")
    parser.add_argument('-d', action="store", dest="allowedDirs", default=defaultAllowedDirs,
                        help="Allowed directories to server files from - comma separated list")
    parser.add_argument('-f', action="store", dest="allowedFileTypes", default=defaultAllowedTypes,
                        help="Allowed file types - comma separated list")
    parser.add_argument('-u', '--username', action="store", dest="username", default=None,
                        help="rtcloud website username")
    parser.add_argument('-p', '--password', action="store", dest="password", default=None,
                        help="rtcloud website password")
    parser.add_argument('--test', default=False, action='store_true',
                         help='Use unsecure non-encrypted connection')
    args = parser.parse_args()

    if not re.match(r'.*:\d+', args.server):
        print("Error: Expecting server address in the form <servername:port>")
        parser.print_help()
        sys.exit()

    if type(args.allowedDirs) is str:
        args.allowedDirs = args.allowedDirs.split(',')

    if type(args.allowedFileTypes) is str:
        args.allowedFileTypes = args.allowedFileTypes.split(',')

    addr, port = args.server.split(':')
    # Check if the ssl certificate is valid for this server address
    if checkSSLCertAltName(certFile, addr) is False:
        # Addr not listed in sslCert, recreate ssl Cert
        makeSSLCertFile(addr)

    print("Server: {}, interval {}".format(args.server, args.interval))
    print("Allowed file types {}".format(args.allowedFileTypes))
    print("Allowed directories {}".format(args.allowedDirs))

    WebSocketFileWatcher.runFileWatcher(args.server,
                                        retryInterval=args.interval,
                                        allowedDirs=args.allowedDirs,
                                        allowedTypes=args.allowedFileTypes,
                                        username=args.username,
                                        password=args.password,
                                        testMode=args.test)
