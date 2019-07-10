import pytest
import os
import sys
import threading
import time
import logging
from base64 import b64decode
import projects.sample.sample as sample
from rtCommon.utils import installLoggers
from rtCommon.fileServer import WebSocketFileWatcher
from web.webServer import Web
import rtCommon.webClientUtils as wcutils
from rtCommon.structDict import StructDict
from rtCommon.readDicom import readDicomFromFile, anonymizeDicom, writeDicomToBuffer


testDir = os.path.dirname(__file__)
tmpDir = os.path.join(testDir, 'tmp/')


@pytest.fixture(scope="module")
def dicomTestFilename():  # type: ignore
    return os.path.join(testDir, 'test_input/001_000005_000100.dcm')


class TestFileWatcher:
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
        time.sleep(1)

        # Start a fileWatcher thread running
        cls.fileThread = threading.Thread(
            name='fileThread',
            target=WebSocketFileWatcher.runFileWatcher,
            args=('localhost:8921',),
            kwargs={
                'retryInterval': 0.5,
                'allowedDirs': ['/tmp', testDir],
                'allowedTypes': ['.dcm', '.mat', '.bin', '.txt'],
                'username': 'test',
                'password': 'test'
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

    def test_ping(cls):
        print("test_ping")
        global pingCallbackEvent
        # Send a ping request from webServer to fileWatcher
        assert Web.wsDataConn is not None
        cmd = {'cmd': 'ping'}
        Web.sendDataMsgFromThread(cmd, timeout=2)

    def test_validateRequestedFile(cls):
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

    def test_getFile(cls, dicomTestFilename):
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
        response = Web.sendDataMsgFromThread(cmd)
        assert response['status'] == 200
        responseData = wcutils.decodeMessageData(response)
        assert responseData == data

        # Try compressed version
        cmd = wcutils.watchFileReqStruct(dicomTestFilename, compress=True)
        response = Web.sendDataMsgFromThread(cmd)
        assert response['status'] == 200
        responseData = wcutils.decodeMessageData(response)
        assert responseData == data

        cmd = wcutils.getFileReqStruct(dicomTestFilename)
        response = Web.sendDataMsgFromThread(cmd)
        assert response['status'] == 200
        responseData = wcutils.decodeMessageData(response)
        assert responseData == data

        # Try compressed version
        cmd = wcutils.getFileReqStruct(dicomTestFilename, compress=True)
        response = Web.sendDataMsgFromThread(cmd)
        assert response['status'] == 200
        responseData = wcutils.decodeMessageData(response)
        assert responseData == data

        cmd = wcutils.getNewestFileReqStruct(dicomTestFilename)
        response = Web.sendDataMsgFromThread(cmd)
        assert response['status'] == 200
        responseData = wcutils.decodeMessageData(response)
        assert responseData == data

        # Try to get a non-allowed file
        cmd = wcutils.getFileReqStruct('/tmp/file.nope')
        response = Web.sendDataMsgFromThread(cmd)
        assert(response['status'] == 400)

        # try from a non-allowed directory
        cmd = wcutils.getFileReqStruct('/nope/file.dcm')
        response = Web.sendDataMsgFromThread(cmd)
        assert(response['status'] == 400)

        # Test putTextFile
        testText = 'hello2'
        textFileName = os.path.join(tmpDir, 'test2.txt')
        cmd = wcutils.putTextFileReqStruct(textFileName, testText)
        response = Web.sendDataMsgFromThread(cmd)
        assert response['status'] == 200

        # Test putBinaryData function
        testData = b'\xFE\xED\x01\x23'
        dataFileName = os.path.join(tmpDir, 'test2.bin')
        cmd = wcutils.putBinaryFileReqStruct(dataFileName, testData, compress=True)
        response = Web.sendDataMsgFromThread(cmd)
        assert response['status'] == 200
        # read back an compare to original
        cmd = wcutils.getFileReqStruct(dataFileName)
        response = Web.sendDataMsgFromThread(cmd)
        responseData = b64decode(response['data'])
        assert responseData == testData

    def test_runFromCommandLine(cls):
        argv = ['--filesremote']
        ret = sample.main(argv)
        assert ret == 0
