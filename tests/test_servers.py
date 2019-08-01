import pytest
import os
import threading
import time
import logging
from base64 import b64decode
import projects.sample.sample as sample
from rtCommon.fileClient import FileInterface
from rtCommon.utils import installLoggers
from rtCommon.fileServer import WebSocketFileWatcher
from web.webServer import Web, handleDataRequest, CommonOutputDir
import rtCommon.webClientUtils as wcutils
from rtCommon.structDict import StructDict
from rtCommon.errors import RequestError
from rtCommon.readDicom import readDicomFromFile, anonymizeDicom, writeDicomToBuffer


testDir = os.path.dirname(__file__)
tmpDir = os.path.join(testDir, 'tmp/')


@pytest.fixture(scope="module")
def dicomTestFilename():  # type: ignore
    return os.path.join(testDir, 'test_input/001_000005_000100.dcm')


@pytest.fixture(scope="module")
def bigTestfile():  # type: ignore
    filename = os.path.join(testDir, 'test_input/bigfile.bin')
    if not os.path.exists(filename):
        with open(filename, 'wb') as fout:
            for i in range(101):
                fout.write(os.urandom(1024*1024))
    return filename


class TestServers:
    webThread = None
    fileThread = None
    pingCount = 0

    def setup_class(cls):
        installLoggers(logging.DEBUG, logging.DEBUG, filename='logs/tests.log')
        # Start a webServer thread running
        params = StructDict({'fmriPyScript': 'projects/sample/sample.py',
                             'filesremote': True,
                             'port': 8921,
                            })
        cfg = StructDict({'sessionId': "test",
                          'subjectName': "test_sample",
                          'subjectNum': 1,
                          'subjectDay': 1,
                          'sessionNum': 1})
        cls.webThread = threading.Thread(name='webThread', target=Web.start, args=(params, cfg, True))
        cls.webThread.setDaemon(True)
        cls.webThread.start()
        time.sleep(.1)

        # Start a fileWatcher thread running
        cls.fileThread = threading.Thread(
            name='fileThread',
            target=WebSocketFileWatcher.runFileWatcher,
            args=('localhost:8921',),
            kwargs={
                'retryInterval': 0.1,
                'allowedDirs': ['/tmp', testDir],
                'allowedTypes': ['.dcm', '.mat', '.bin', '.txt'],
                'username': 'test',
                'password': 'test',
                'testMode': True
            }
        )
        cls.fileThread.setDaemon(True)
        cls.fileThread.start()
        time.sleep(1)

    def teardown_class(cls):
        WebSocketFileWatcher.stop()
        Web.stop()
        time.sleep(1)
        pass

    def test_ping(self):
        print("test_ping")
        global pingCallbackEvent
        # Send a ping request from webServer to fileWatcher
        assert Web.wsDataConn is not None
        cmd = {'cmd': 'ping'}
        Web.sendDataMsgFromThread(cmd, timeout=2)

    def test_validateRequestedFile(self):
        print("test_validateRequestedFile")
        res = WebSocketFileWatcher.validateRequestedFile('/tmp/data', None)
        assert res is True

        res = WebSocketFileWatcher.validateRequestedFile('/tmp/data', 'file.dcm')
        assert res is True

        res = WebSocketFileWatcher.validateRequestedFile('/tmp/data', 'file.not')
        assert res is False

        res = WebSocketFileWatcher.validateRequestedFile('/sys/data', 'file.dcm')
        assert res is False

        res = WebSocketFileWatcher.validateRequestedFile(None, '/tmp/data/file.dcm')
        assert res is True

        res = WebSocketFileWatcher.validateRequestedFile(None, '/sys/data/file.dcm')
        assert res is False

        res = WebSocketFileWatcher.validateRequestedFile(None, '/tmp/file.bin')
        assert res is True

        res = WebSocketFileWatcher.validateRequestedFile(None, '/tmp/file.txt')
        assert res is True

    def test_getFile(self, dicomTestFilename):
        print("test_getFile")
        global fileData
        assert Web.wsDataConn is not None
        # Try to initialize file watcher with non-allowed directory
        cmd = wcutils.initWatchReqStruct('/', '*', 0)
        response = Web.sendDataMsgFromThread(cmd)
        # we expect an error because '/' directory not allowed
        assert response['status'] == 400

        # Initialize with allowed directory
        cmd = wcutils.initWatchReqStruct(testDir, '*.dcm', 0)
        response = Web.sendDataMsgFromThread(cmd)
        assert response['status'] == 200

        dcmImg = readDicomFromFile(dicomTestFilename)
        anonDcm = anonymizeDicom(dcmImg)
        data = writeDicomToBuffer(anonDcm)
        # with open(dicomTestFilename, 'rb') as fp:
        #     data = fp.read()

        cmd = wcutils.watchFileReqStruct(dicomTestFilename)
        try:
            responseData = handleDataRequest(cmd)
        except Exception as err:
            assert False, str(err)
        # import pdb; pdb.set_trace()
        assert responseData == data

        # Try compressed version
        cmd = wcutils.watchFileReqStruct(dicomTestFilename, compress=True)
        try:
            responseData = handleDataRequest(cmd)
        except Exception as err:
            assert False, str(err)
        assert responseData == data

        cmd = wcutils.getFileReqStruct(dicomTestFilename)
        try:
            responseData = handleDataRequest(cmd)
        except Exception as err:
            assert False, str(err)
        assert responseData == data

        # Try compressed version
        cmd = wcutils.getFileReqStruct(dicomTestFilename, compress=True)
        try:
            responseData = handleDataRequest(cmd)
        except Exception as err:
            assert False, str(err)
        assert responseData == data

        cmd = wcutils.getNewestFileReqStruct(dicomTestFilename)
        try:
            responseData = handleDataRequest(cmd)
        except Exception as err:
            assert False, str(err)
        assert responseData == data

        # Try to get a non-allowed file
        cmd = wcutils.getFileReqStruct('/tmp/file.nope')
        try:
            responseData = handleDataRequest(cmd)
        except RequestError as err:
            # Expecting a status not 200 error to be raised
            assert 'status' in str(err)
        else:
            self.fail('Expecting RequestError')

        # try from a non-allowed directory
        cmd = wcutils.getFileReqStruct('/nope/file.dcm')
        try:
            responseData = handleDataRequest(cmd)
        except RequestError as err:
            # Expecting a status not 200 error to be raised
            assert 'status' in str(err)
        else:
            self.fail('Expecting RequestError')

        # Test putTextFile
        testText = 'hello2'
        textFileName = os.path.join(tmpDir, 'test2.txt')
        cmd = wcutils.putTextFileReqStruct(textFileName, testText)
        response = Web.sendDataMsgFromThread(cmd)
        assert response['status'] == 200

        # Test putBinaryData function
        testData = b'\xFE\xED\x01\x23'
        dataFileName = os.path.join(tmpDir, 'test2.bin')
        cmd = wcutils.putBinaryFileReqStruct(dataFileName)
        for putFilePart in wcutils.generateDataParts(testData, cmd, compress=True):
            response = Web.sendDataMsgFromThread(putFilePart)
        assert response['status'] == 200
        # read back an compare to original
        cmd = wcutils.getFileReqStruct(dataFileName)
        response = Web.sendDataMsgFromThread(cmd)
        responseData = b64decode(response['data'])
        assert responseData == testData

    def test_getBigFile(self, bigTestfile):
        # Read in original data
        with open(bigTestfile, 'rb') as fp:
            data = fp.read()

        # Read via fileClient
        startTime = time.time()
        cmd = wcutils.getFileReqStruct(bigTestfile)
        try:
            responseData = handleDataRequest(cmd)
        except Exception as err:
            assert False, str(err)
        assert responseData == data
        print('Read Bigfile time: {}'.format(time.time() - startTime))

        # Write bigFile
        startTime = time.time()
        cmd = wcutils.putBinaryFileReqStruct(bigTestfile)
        i = 0
        for putFilePart in wcutils.generateDataParts(data, cmd, compress=False):
            i += 1
            response = Web.sendDataMsgFromThread(putFilePart)
            assert response['status'] == 200
        print('Write Bigfile time: {}'.format(time.time() - startTime))

        # Read back written data
        writtenPath = os.path.join(CommonOutputDir, bigTestfile)
        with open(writtenPath, 'rb') as fp:
            writtenData = fp.read()
        assert writtenData == data

    def test_runFromCommandLine(self):
        argv = ['--filesremote']
        ret = sample.main(argv)
        assert ret == 0

    def test_fileInterface(self, bigTestfile):
        webComm = wcutils.initWebPipeConnection(None, True)
        fileInterface = FileInterface(filesremote=True, webpipes=webComm)

        # Read in original data
        with open(bigTestfile, 'rb') as fp:
            data = fp.read()

        # Read via fileClient
        startTime = time.time()
        try:
            responseData = fileInterface.getFile(bigTestfile)
        except Exception as err:
            assert False, str(err)
        assert responseData == data
        print('Read Bigfile time: {}'.format(time.time() - startTime))

        # Write bigFile
        startTime = time.time()
        try:
            fileInterface.putBinaryFile(bigTestfile, data)
        except Exception as err:
            assert False, str(err)
        print('Write Bigfile time: {}'.format(time.time() - startTime))
        # Read back written data and compare to original
        writtenPath = os.path.join(CommonOutputDir, bigTestfile)
        with open(writtenPath, 'rb') as fp:
            writtenData = fp.read()
        assert writtenData == data
