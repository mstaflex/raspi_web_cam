from RPIO import PWM
import zmq


servo = PWM.Servo()

port = "5556"
context = zmq.Context()
socket = context.socket(zmq.PAIR)
socket.bind("tcp://*:%s" % port)

while True:
    msg = socket.recv()
    port, pulse = int(msg.split(",")[0]), int(msg.split(",")[1])
    servo.set_servo(port, pulse)
    socket.send("ok")
