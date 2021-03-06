#!/usr/bin/env python
from time import time

from cv_bridge import CvBridge
from duckietown_msgs.msg import SegmentList, LanePose, BoolStamped, Twist2DStamped, FSMState
from duckietown_utils.instantiate_utils import instantiate
import numpy as np
import rospy
from sensor_msgs.msg import Image
from std_msgs.msg import Float32, String
import json

class LineDetectorNode(object):

    def __init__(self):
        self.node_name = "Slim Parking"
        self.active = False
        self.filter = None
        self.updateParams(None)
        self.active_mode = False

        self.t_last_update = rospy.get_time()
        self.velocity = Twist2DStamped()


        self.d_median = []
        self.phi_median = []
        self.latencyArray = []

        self.park_timeout = 10
        rospy.set_param("~park_timeout", self.park_timeout)


        # Define Constants
        self.curvature_res = self.filter.curvature_res

        # Set parameters to server
        rospy.set_param('~curvature_res', self.curvature_res) #Write to parameter server for transparancy

        self.pub_in_lane = rospy.Publisher("~in_lane_parking",BoolStamped, queue_size=1)
        # Subscribers
        self.sub = rospy.Subscriber("/articuno/ground_projection/lineseglist_out", SegmentList, self.processSegments, queue_size=1)
        self.sub_velocity = rospy.Subscriber("/articuno/car_cmd_switch_node/cmd", Twist2DStamped, self.updateVelocity)
        self.sub_change_params = rospy.Subscriber("/articuno/lane_filter_node/change_params", String, self.cbChangeParams)
        # Publishers
        self.pub_lane_pose = rospy.Publisher("/articuno/lane_filter_node/lane_pose", LanePose, queue_size=1)
        self.pub_belief_img = rospy.Publisher("/articuno/lane_filter_node/belief_img", Image, queue_size=1)
        self.pub_seglist_filtered = rospy.Publisher("/articuno/lane_filter_node/seglist_filtered",SegmentList, queue_size=1)

        self.pub_ml_img = rospy.Publisher("/articuno/lane_filter_node/ml_img", Image, queue_size=1)


        self.pub_entropy    = rospy.Publisher("/articuno/lane_filter_node/entropy",Float32, queue_size=1)

        self.pub_parking_detection = rospy.Publisher("~parking_line", BoolStamped, queue_size=1)

        self.pub_parking_on = rospy.Publisher("/articuno/parking_on", BoolStamped,queue_size=1 )

        self.pub_exit_from_parking = rospy.Publisher("~exit_from_parking" , BoolStamped,queue_size=1)

        # FSM
        self.sub_switch = rospy.Subscriber("~switch",BoolStamped, self.cbSwitch, queue_size=1)
        self.sub_fsm_mode = rospy.Subscriber("/articuno/fsm_node/mode", FSMState, self.cbMode, queue_size=1)

        # timer for updating the params
        self.timer = rospy.Timer(rospy.Duration.from_sec(1.0), self.updateParams)

    def cbChangeParams(self, msg):
        data = json.loads(msg.data)
        params = data["params"]
        reset_time = data["time"]
        # Set all paramters which need to be updated
        for param_name in params.keys():
            param_val = params[param_name]
            params[param_name] = eval("self.filter." + str(param_name))
            exec("self.filter." + str(param_name) + "=" + str(param_val))

        # Sleep for reset time
        rospy.sleep(reset_time)

        # Reset parameters to old values
        for param_name in params.keys():
            param_val = params[param_name]
            exec("self.filter." + str(param_name) + "=" + str(param_val))

    def updateParams(self, event):
        if self.filter is None:
            c = rospy.get_param('~filter')
            assert isinstance(c, list) and len(c) == 2, c

            self.loginfo('new filter config: %s' % str(c))
            self.filter = instantiate(c[0], c[1])

    def cbSwitch(self, switch_msg):
        self.active = switch_msg.data


    def cbMode(self, msg):
        if msg.state == "SLIM_PARKING":
            rospy.set_param('/articuno/lane_controller_node/v_bar', 0.14)
            rospy.set_param('/articuno/lane_controller_node/k_theta', -0.5)
            rospy.set_param('/articuno/stop_line_filter_node/stop_distance', 0.15)
            self.active_mode = True


        #if msg.state == "LOOKING_FOR_PARKING":

        if msg.state == "PARKED":
            rospy.sleep(self.park_timeout/2)
            park_timeout_expired = BoolStamped()
            #park_timeout_expired.header.stamp = 0
            park_timeout_expired.data = True
            self.pub_exit_from_parking.publish(park_timeout_expired)
            #self.wait(self.park_timeout)

        if msg.state == "EXITING_FROM_PARKING":
            rospy.set_param('/articuno/lane_controller_node/v_bar', -0.05)
            rospy.set_param('/articuno/lane_controller_node/k_theta', -0.04)

        if msg.state == "LANE_FOLLOWING":
            rospy.set_param('/articuno/lane_controller_node/v_bar', 0.23)
            rospy.set_param('/articuno/lane_controller_node/k_theta', -1)
            rospy.set_param('/articuno/stop_line_filter_node/stop_distance', 0.22)
            self.filter.parking_detected = False
            self.active_mode = False


    def processSegments(self,segment_list_msg):
        if not self.active:
            return
        # Get actual timestamp for latency measurement
        timestamp_now = rospy.Time.now()

        # TODO-TAL double call to param server ... --> see TODO in the readme, not only is it never updated, but it is alwas 0
        # Step 0: get values from server
        if (rospy.get_param('~curvature_res') is not self.curvature_res):
            self.curvature_res = rospy.get_param('~curvature_res')
            self.filter.updateRangeArray(self.curvature_res)

        # Step 1: predict
        current_time = rospy.get_time()
        dt = current_time - self.t_last_update
        v = self.velocity.v
        w = self.velocity.omega

        self.filter.predict(dt=dt, v=v, w=w)
        self.t_last_update = current_time

        # Step 2: update

        self.filter.update(segment_list_msg.segments)

        parking_detected = BoolStamped()
        parking_detected.header.stamp = segment_list_msg.header.stamp
        parking_detected.data = self.filter.parking_detected
        self.pub_parking_detection.publish(parking_detected)

        parking_on = BoolStamped()
        parking_on.header.stamp = segment_list_msg.header.stamp
        parking_on.data = True
        self.pub_parking_on.publish(parking_on)

        if self.active_mode:
            # Step 3: build messages and publish things
            [d_max, phi_max] = self.filter.getEstimate()
            # print "d_max = ", d_max
            # print "phi_max = ", phi_max

            inlier_segments = self.filter.get_inlier_segments(segment_list_msg.segments, d_max, phi_max)
            inlier_segments_msg = SegmentList()
            inlier_segments_msg.header = segment_list_msg.header
            inlier_segments_msg.segments = inlier_segments
            self.pub_seglist_filtered.publish(inlier_segments_msg)


            #max_val = self.filter.getMax()
            #in_lane = max_val > self.filter.min_max
            # build lane pose message to send
            lanePose = LanePose()
            lanePose.header.stamp = segment_list_msg.header.stamp
            lanePose.d = d_max[0]
            lanePose.phi = phi_max[0]
            #lanePose.in_lane = in_lane
            # XXX: is it always NORMAL?
            lanePose.status = lanePose.NORMAL

            if self.curvature_res > 0:
                lanePose.curvature = self.filter.getCurvature(d_max[1:], phi_max[1:])

            self.pub_lane_pose.publish(lanePose)

            # TODO-TAL add a debug param to not publish the image !!
            # TODO-TAL also, the CvBridge is re instantiated every time...
            # publish the belief image
            bridge = CvBridge()
            belief_img = bridge.cv2_to_imgmsg(np.array(255 * self.filter.beliefArray[0]).astype("uint8"), "mono8")
            belief_img.header.stamp = segment_list_msg.header.stamp
            self.pub_belief_img.publish(belief_img)



            # Latency of Estimation including curvature estimation
            estimation_latency_stamp = rospy.Time.now() - timestamp_now
            estimation_latency = estimation_latency_stamp.secs + estimation_latency_stamp.nsecs/1e9
            self.latencyArray.append(estimation_latency)

            if (len(self.latencyArray) >= 20):
                self.latencyArray.pop(0)

            # print "Latency of segment list: ", segment_latency
            # print("Mean latency of Estimation:................. %s" % np.mean(self.latencyArray))

            # also publishing a separate Bool for the FSM
            in_lane_msg = BoolStamped()
            in_lane_msg.header.stamp = segment_list_msg.header.stamp
            in_lane_msg.data = self.filter.inLane
            if (self.filter.inLane):
                self.pub_in_lane.publish(in_lane_msg)


    def updateVelocity(self,twist_msg):
        self.velocity = twist_msg

    def onShutdown(self):
        rospy.loginfo("[LineDetectorNode] Shutdown.")


    def loginfo(self, s):
        rospy.loginfo('[%s] %s' % (self.node_name, s))


if __name__ == '__main__':
    rospy.init_node('slim_parking', anonymous=False)
    lane_filter_node = LineDetectorNode()
    rospy.on_shutdown(lane_filter_node.onShutdown)
    rospy.spin()

