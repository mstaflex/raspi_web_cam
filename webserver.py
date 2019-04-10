import tornado
import tornado.ioloop
import tornado.web
import tornado.escape
import tornado.auth
from tornado.concurrent import Future
from tornado import gen
from tornado.options import define, options
import functools
import os
import io
import datetime
import threading
import logging
import time
import numpy as np
import cv2
import picamera
#from RPIO import PWM
import zmq


define("port", default=8888, help="run on the given port", type=int)
define("local_ip_prefix", default="191.168.178", help="beginning of an internal IP address", type=str)
define("secure_user", default="raspicam_user", help="Cookie user name for login", type=str)
define("index_file_path", default="content/index.html", help="path to the index.html file", type=str)
define("login_url", default="/auth/login", help="login as a user url", type=str)
define("password", default="PUT_YOUR_SECRET_HERE", help="Password to login", type=str)



logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

authorized_user_emails = ['some.mail_address@gmail.com']
requested_access = []


class PiCam:
    def __init__(self):
        self.camera = picamera.PiCamera()
        self.camera.resolution = (640, 480)
        self.camera.framerate = 2
        self.log = logging.getLogger('PiCam')
        self.stream = io.BytesIO()  

    def getPic(self):
        start_time = time.time()
        self.camera.capture(self.stream, format='jpeg')
        return self.post_process()

    def get_gen(self, framerate=2):
        for foo in self.camera.capture_continuous(self.stream, 'jpeg', use_video_port=True):
            #self.log.info("Accquiring image from PiCam took %.2fs" % (time_span))
            last_yield = time.time()
            yield self.post_process()

    def post_process(self):
        start_time = time.time()
        self.stream.seek(0)
        output = self.stream.read()
        self.stream.seek(0)
        self.stream.truncate()
        data = np.fromstring(output, dtype=np.uint8)
        image = cv2.imdecode(data, 1)
        ts = datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
        cv2.rectangle(image, (0, 0), (0 + 640 , 0 + 10), (0,0,0), 10) 
        cv2.putText(image, "Last image: %s" % (ts), (10,11), cv2.FONT_HERSHEY_PLAIN, 1, (255,255,255))
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 60]
        result, image = cv2.imencode('.jpg', image, encode_param)
        image = image.tostring()
        time_span = time.time() - start_time
        #self.log.info("Accquiring image from PiCam took %.2fs" % (time_span)) 
        return image


class YieldQueue(object):
    def __init__(self):
        self.queue = []
        self.log = logging.getLogger("YieldQueue")

    def get_future(self):
        result_future = Future()
        self.queue.append(result_future)
        return result_future

    def cancel_wait(self, future):
        self.remove_future(future)
        future.set_result('cancel')

    def new_result(self, result):
        self.log.info("waiters: %d" % (len(self.queue)))
        for future in self.queue:
            future.set_result(result)

    def remove_future(self, future):
        if future in self.queue:
            self.queue.remove(future)
        else:
            self.log.warn("Future object was not found in queue")


waiters = YieldQueue()
cam = PiCam()

def getPicThread():
    while True:
        try:
            #image = cam.getPic()
            timestamp = 0
            for image in cam.get_gen():
                waiters.new_result(image)
                span = time.time() - timestamp
                if span < 0.5:
                    time.sleep(0.5 - span)
                timestamp = time.time()
        except Exception as e:
            time.sleep(3.)
            raise

t = threading.Thread(target=getPicThread)
t.start()

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def check_authenticated(method):
    """Decorate methods with this to check if current user has access."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        #if not self.request.remote_ip.startswith(options.local_ip_prefix):
        if not self.current_user == options.password: 
                raise tornado.web.HTTPError(403)
        return method(self, *args, **kwargs)
    return wrapper

# ---------------------------------------------------------------------------

class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        #if self.request.remote_ip.startswith(options.local_ip_prefix):
        #    #TODO: Can this be deleted? We check in decorator authenticated_from_external if user has access, right?
        #    return dict(name="Internal User")
        user = self.get_secure_cookie(options.secure_user)
        if not user:
            return None
        return "external_user"

class IndexHandler(BaseHandler):
    def get(self):
        if not self.current_user:
            self.redirect(options.login_url)
        self.render(options.index_file_path)

class DefaultHandler(BaseHandler):
    def get(self):
        self.redirect(r"./index.html")

class AuthRejectHandler(BaseHandler):
    def get(self):
        self.write("Rejected you jerk!")

class AuthHandler(BaseHandler):
    def get(self):
        self.write('<html><body><form action=%s method="post">'
                   'Name: <input type="text" name="password">'
                   '<input type="submit" value="Sign in">'
                   '</form></body></html>' % (options.login_url))

    def post(self):
        if self.get_argument("password") == options.password:
            self.set_secure_cookie(options.secure_user, options.password)
        self.redirect("/")

class ServoHandler(BaseHandler):
    def initialize(self, **kwargs):
        self.log = logging.getLogger("ServoHandler")
        self.valid_gpio_outputs = kwargs['valid_gpio_outputs']
        self.valid_pulse_range = kwargs['valid_pulse_range']
        self.pulse_port = kwargs['pulse_port']
        #  Prepare our context and sockets
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PAIR)
        self.socket.connect("tcp://localhost:%s" % self.pulse_port)

    def prepare(self):
        self.pulse = int(self.request.path.split('/')[-1])
        self.gpio = int(self.request.path.split('/')[-2])
        self.log.info("port %d pulse %d" % (self.gpio, self.pulse))
        if not self.gpio in self.valid_gpio_outputs:
            self.send_error(404)
        if not self.pulse in self.valid_pulse_range:
            self.pulse = min(max(self.pulse, self.valid_pulse_range[0]), self.valid_pulse_range[1])

    #@check_authenticated
    def get(self):
        self.socket.send(b"%d,%d" % (self.gpio, self.pulse))
        message = self.socket.recv()
        if message == "ok":
            self.log.info("Successfully requested servo control")
            self.write("Ok")
        else:
            self.log.error("Error from servo control! %s" % (message))

class CamHandler(BaseHandler):
    def initialize(self, **kwargs):
        self.yield_queue = kwargs["queue"]
        self.log = logging.getLogger("CamHandler")

    #@check_authenticated
    @tornado.web.asynchronous
    @gen.coroutine
    def get(self):
        self.log.info("Incoming GET request from ")
        self.run = True
        ioloop = tornado.ioloop.IOLoop.current()

        my_boundary = "--boundarydonotcross"
        self.set_header('Cache-Control', 'no-store, no-cache, must-revalidate, pre-check=0, post-check=0, max-age=0')
        self.set_header('Connection', 'close')
        self.set_header( 'Content-Type', 'multipart/x-mixed-replace;boundary=%s' % (my_boundary))
        self.set_header('Expires', 'Mon, 3 Jan 2000 12:34:56 GMT')
        self.set_header( 'Pragma', 'no-cache')  

        while self.run:
            self.future = self.yield_queue.get_future()
            yield self.future
            self.yield_queue.remove_future(self.future)
            result_photo = self.future.result()
            if type(result_photo) == str and result_photo == "cancel":
                self.log.warn("This stream is requested to close.. Ending..")
                break
            elif result_photo == []:
                self.log.warn("No photo received.. empty list.. invalid state!")
            self.write(my_boundary + "\r\n")
            self.write("Content-type: image/jpeg\r\n")
            self.write("Content-length: %s\r\n" % len(result_photo))
            self.write("X-Timestamp: %f\r\n\r\n" % time.time())
            self.write(result_photo)
            self.flush()

    def on_connection_close(self):
        self.log.info("Connection closed...")
        self.run = False
        self.yield_queue.cancel_wait(self.future)


application = tornado.web.Application([
    (r'/', DefaultHandler),
    (r'/cam', CamHandler, {"queue": waiters}),
    (options.login_url, AuthHandler),
    (r'/index.html', IndexHandler),
    (r'/reject.html', AuthRejectHandler),
    (r'/static/(.*)', tornado.web.StaticFileHandler, {'path': 'content/static'}),
    (r'/servo/[0-9]*/[0-9]*', ServoHandler, {'valid_gpio_outputs': [9, 10], 'valid_pulse_range': [600, 2000], 'pulse_port': 5556}),
], cookie_secret="ENTER_YOUR_SECRET_HERE",
login_url="/auth/login")


if __name__ == "__main__":
    application.listen(options.port, "0.0.0.0")
    tornado.ioloop.IOLoop.instance().start()