from collections import deque
import threading
import time
import traceback

import numpy as np
import cv2
from simplecv.core.camera.frame_source import FrameSource
from simplecv.factory import Factory

VIMBA_ENABLED = True
try:
    import pymba
except ImportError:
    #TODO Log an error the pymba is not installed
    VIMBA_ENABLED = False
except Exception:
    #TODO Log an error that AVT Vimba DLL is not installed properly
    VIMBA_ENABLED = False


class VimbaCameraThread(threading.Thread):

    camera = None
    run = True
    verbose = False
    lock = None
    logger = None
    framerate = 0

    def __init__(self, camera):
        super(VimbaCameraThread, self).__init__()
        self._stop = threading.Event()
        self.camera = camera
        self.lock = threading.Lock()
        self.name = 'Thread-Camera-ID-' + str(self.camera.uniqueid)

    def run(self):
        counter = 0
        timestamp = time.time()

        while self.run:
            self.lock.acquire()

            img = self.camera._captureFrame(1000)
            self.camera._buffer.appendleft(img)

            self.lock.release()
            counter += 1
            time.sleep(0.01)

            if time.time() - timestamp >= 1:
                self.camera.framerate = counter
                counter = 0
                timestamp = time.time()

    def stop(self):
        self._stop.set()

    def stopped(self):
        return self._stop.isSet()


class VimbaCamera(FrameSource):
    """
    **SUMMARY**
    VimbaCamera is a wrapper for the Allied Vision cameras,
    such as the "manta" series.

    This requires the
    1) Vimba SDK provided from Allied Vision
    http://www.alliedvisiontec.com/us/products/software/vimba-sdk.html

    2) Pyvimba Python library
    TODO: <INSERT URL>

    Note that as of time of writing, the VIMBA driver is not available
    for Mac.

    All camera properties are directly from the Vimba SDK manual -- if not
    specified it will default to whatever the camera state is.  Cameras
    can either by

    **EXAMPLE**
    >>> cam = VimbaCamera(0, {"width": 656, "height": 492})
    >>>
    >>> img = cam.getImage()
    >>> img.show()
    """

    def _setupVimba(self):
        from pymba import Vimba

        self._vimba = Vimba()
        self._vimba.startup()
        system = self._vimba.getSystem()
        if system.GeVTLIsPresent:
            system.runFeatureCommand("GeVDiscoveryAllOnce")
            time.sleep(0.2)

    def __del__(self):
        #This function should disconnect from the Vimba Camera
        if self._camera is not None:
            if self.threaded:
                self._thread.stop()
                time.sleep(0.2)

            if self._frame is not None:
                self._frame.revokeFrame()
                self._frame = None

            self._camera.closeCamera()

        self._vimba.shutdown()

    def shutdown(self):
        """You must call this function if you are using threaded=true when you are finished
            to prevent segmentation fault"""
        # REQUIRED TO PREVENT SEGMENTATION FAULT FOR THREADED=True
        if (self._camera):
            self._camera.closeCamera()

        self._vimba.shutdown()


    def __init__(self, camera_id = -1, properties = {}, threaded = False):
        if not VIMBA_ENABLED:
            raise Exception("You don't seem to have the pymba library installed.  This will make it hard to use a AVT Vimba Camera.")

        self._vimba = None
        self._setupVimba()

        camlist = self.listAllCameras()
        self._camTable = {}
        self._frame = None
        self._buffer = None # Buffer to store images
        self._buffersize = 10 # Number of images to keep in the rolling image buffer for threads
        self._lastimage = None # Last image loaded into memory
        self._thread = None
        self._framerate = 0
        self.threaded = False
        self._properties = {}
        self._camera = None

        i = 0
        for cam in camlist:
            self._camTable[i] = {'id': cam.cameraIdString}
            i += 1

        if not len(camlist):
            raise Exception("Couldn't find any cameras with the Vimba driver.  Use VimbaViewer to confirm you have one connected.")

        if camera_id < 9000: #camera was passed as an index reference
            if camera_id == -1:  #accept -1 for "first camera"
                camera_id = 0

            if (camera_id > len(camlist)):
                raise Exception("Couldn't find camera at index %d." % camera_id)

            cam_guid = camlist[camera_id].cameraIdString
        else:
            raise Exception("Index %d is too large" % camera_id)

        self._camera = self._vimba.getCamera(cam_guid)
        self._camera.openCamera()

        self.uniqueid = cam_guid

        self.setProperty("AcquisitionMode","SingleFrame")
        self.setProperty("TriggerSource","Freerun")

        # TODO: FIX
        if properties.get("mode", "RGB") == 'gray':
            self.setProperty("PixelFormat", "Mono8")
        else:
            fmt = "RGB8Packed" # alternatively use BayerRG8
            self.setProperty("PixelFormat", "BayerRG8")

        #give some compatablity with other cameras
        if properties.get("mode", ""):
            properties.pop("mode")

        if properties.get("height", ""):
            properties["Height"] = properties["height"]
            properties.pop("height")

        if properties.get("width", ""):
            properties["Width"] = properties["width"]
            properties.pop("width")

        for p in properties:
            self.setProperty(p, properties[p])

        if threaded:
          self._thread = VimbaCameraThread(self)
          self._thread.daemon = True
          self._buffer = deque(maxlen=self._buffersize)
          self._thread.start()
          self.threaded = True

        self._refreshFrameStats()

    def restart(self):
        """
        This tries to restart the camera thread
        """
        self._thread.stop()
        self._thread = VimbaCameraThread(self)
        self._thread.daemon = True
        self._buffer = deque(maxlen=self._buffersize)
        self._thread.start()

    def listAllCameras(self):
        """
        **SUMMARY**
        List all cameras attached to the host

        **RETURNS**
        List of VimbaCamera objects, otherwise empty list
        VimbaCamera objects are defined in the pymba module
        """
        cameraIds = self._vimba.getCameraIds()
        ar = []
        for cameraId in cameraIds:
            ar.append(self._vimba.getCamera(cameraId))
        return ar

    def runCommand(self,command):
        """
        **SUMMARY**
        Runs a Vimba Command on the camera

        Valid Commands include:
        * AcquisitionAbort
        * AcquisitionStart
        * AcquisitionStop

        **RETURNS**

        0 on success

        **EXAMPLE**
        >>>c = VimbaCamera()
        >>>c.runCommand("TimeStampReset")
        """
        return self._camera.runFeatureCommand(command)

    def getProperty(self, name):
        """
        **SUMMARY**
        This retrieves the value of the Vimba Camera attribute

        There are around 140 properties for the Vimba Camera, so reference the
        Vimba Camera pdf that is provided with
        the SDK for detailed information

        Throws VimbaException if property is not found or not implemented yet.

        **EXAMPLE**
        >>>c = VimbaCamera()
        >>>print c.getProperty("ExposureMode")
        """
        return self._camera.__getattr__(name)

    #TODO, implement the PvAttrRange* functions
    #def getPropertyRange(self, name)

    def getAllProperties(self):
        """
        **SUMMARY**
        This returns a dict with the name and current value of the
        documented Vimba attributes

        CAVEAT: it addresses each of the properties individually, so
        this may take time to run if there's network latency

        **EXAMPLE**
        >>>c = VimbaCamera(0)
        >>>props = c.getAllProperties()
        >>>print props['ExposureMode']

        """
        from pymba import VimbaException

        # TODO
        ar = {}
        c = self._camera
        cameraFeatureNames = c.getFeatureNames()
        for name in cameraFeatureNames:
            try:
                ar[name] =  c.__getattr__(name)
            except VimbaException:
                # Ignore features not yet implemented
                pass
        return ar


    def setProperty(self, name, value, skip_buffer_size_check=False):
        """
        **SUMMARY**
        This sets the value of the Vimba Camera attribute.

        There are around 140 properties for the Vimba Camera, so reference the
        Vimba Camera pdf that is provided with
        the SDK for detailed information

        Throws VimbaException if property not found or not yet implemented

        **Example**
        >>>c = VimbaCamera()
        >>>c.setProperty("ExposureAutoRate", 200)
        >>>c.getImage().show()
        """
        ret = self._camera.__setattr__(name, value)

        #just to be safe, re-cache the camera metadata
        if not skip_buffer_size_check:
            self._refreshFrameStats()

        return ret

    def getImage(self):
        """
        **SUMMARY**
        Extract an Image from the Camera, returning the value.  No matter
        what the image characteristics on the camera, the Image returned
        will be RGB 8 bit depth, if camera is in greyscale mode it will
        be 3 identical channels.

        **EXAMPLE**
        >>>c = VimbaCamera()
        >>>c.getImage().show()
        """

        if self.threaded:
            self._thread.lock.acquire()
            try:
                img = self._buffer.pop()
                self._lastimage = img
            except IndexError:
                img = self._lastimage
            self._thread.lock.release()

        else:
            img = self._captureFrame()

        return img

    def setupASyncMode(self):
        self.setProperty('AcquisitionMode','SingleFrame')
        self.setProperty('TriggerSource','Software')

    def setupSyncMode(self):
        self.setProperty('AcquisitionMode','SingleFrame')
        self.setProperty('TriggerSource','Freerun')

    def _refreshFrameStats(self):
        self.width = self.getProperty("Width")
        self.height = self.getProperty("Height")
        self.pixelformat = self.getProperty("PixelFormat")
        self.imgformat = 'RGB'
        if self.pixelformat == 'Mono8':
            self.imgformat = 'L'

    def _getFrame(self):
        if not self._frame:
            self._frame = self._camera.getFrame()    # creates a frame
            self._frame.announceFrame()

        return self._frame

    def _captureFrame(self, timeout = 5000):
        try:
            c = self._camera
            f = self._getFrame()

            colorSpace = Factory.Image.BGR
            if self.pixelformat == 'Mono8':
                colorSpace = Factory.Image.GRAY

            c.startCapture()
            f.queueFrameCapture()
            c.runFeatureCommand('AcquisitionStart')
            c.runFeatureCommand('AcquisitionStop')
            try:
                f.waitFrameCapture(timeout)
            except Exception, e:
                print "Exception waiting for frame: %s: %s" % (e, traceback.format_exc())
                raise(e)

            imgData = f.getBufferByteData()
            moreUsefulImgData = np.ndarray(buffer = imgData,
                                           dtype = np.uint8,
                                           shape = (f.height, f.width, 1))

            rgb = cv2.cvtColor(moreUsefulImgData, cv2.COLOR_BAYER_RG2RGB)
            c.endCapture()

            return Factory.Image(rgb, colorSpace=colorSpace)

        except Exception, e:
            print "Exception acquiring frame: %s: %s" % (e, traceback.format_exc())
            raise(e)
