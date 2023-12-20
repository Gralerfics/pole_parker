#!/usr/bin/env python

import time
import yaml
import numpy as np
from scipy.ndimage import distance_transform_cdt

import rospy
import actionlib

from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
from geometry_msgs.msg import Pose, PoseWithCovariance, PoseWithCovarianceStamped
from sensor_msgs.msg import LaserScan
from nav_msgs.msg import Odometry

from std_srvs.srv import Empty


class MyMovebaseClient(actionlib.SimpleActionClient):
    def __init__(self, initial = (0, 0, 0)):
        super().__init__('move_base', MoveBaseAction)
        
        # wait for action server
        self.wait_for_server()
        
        # initial pose publisher
        self.initialpose_pub = rospy.Publisher('/initialpose', PoseWithCovarianceStamped, queue_size = 10)
        initial_pose = PoseWithCovarianceStamped()
        initial_pose.header.frame_id = 'map'
        initial_pose.header.stamp = rospy.Time.now()
        if len(initial) == 3:
            initial_pose.pose.pose = self.xytheta_to_pose(*initial)
        elif len(initial) == 4:
            initial_pose.pose.pose = self.xytheta_to_pose(initial[0], initial[1], 0)
            initial_pose.pose.pose.orientation.z = initial[2]
            initial_pose.pose.pose.orientation.w = initial[3]
        
        # amcl_pose subscriber
        rospy.Subscriber('/amcl_pose', PoseWithCovarianceStamped, self.amcl_pose_callback)
        self.amcl_pose = None
        
        # publish initial pose and wait for amcl_pose
        while not rospy.is_shutdown():
            rospy.loginfo('Publishing /initialpose, waiting for /amcl_pose ...')
            self.initialpose_pub.publish(initial_pose)
            # self.amcl_pose = rospy.wait_for_message('/amcl_pose', PoseWithCovarianceStamped, timeout = 1.0)
            if self.amcl_pose is not None:
                dx = self.amcl_pose.pose.pose.position.x - initial_pose.pose.pose.position.x
                dy = self.amcl_pose.pose.pose.position.y - initial_pose.pose.pose.position.y
                dtheta = np.arctan2(self.amcl_pose.pose.pose.orientation.z, self.amcl_pose.pose.pose.orientation.w) * 2 - np.arctan2(initial_pose.pose.pose.orientation.z, initial_pose.pose.pose.orientation.w) * 2
                if np.sqrt(dx ** 2 + dy ** 2) < 0.1 and np.mod(abs(dtheta), 2 * np.pi) < np.deg2rad(5):
                    break
        
        # clear costmap
        rospy.wait_for_service('/move_base/clear_costmaps')
        clear_costmaps_client = rospy.ServiceProxy('/move_base/clear_costmaps', Empty)
        response = clear_costmaps_client()
        time.sleep(1.0)
    
    def amcl_pose_callback(self, msg):
        self.amcl_pose = msg
    
    def get_robot_xytheta(self): # -> (x, y, theta)
        return (self.amcl_pose.pose.pose.position.x, self.amcl_pose.pose.pose.position.y, np.arctan2(self.amcl_pose.pose.pose.orientation.z, self.amcl_pose.pose.pose.orientation.w) * 2)
    
    def xytheta_to_pose(self, x, y, theta) -> Pose:
        res = Pose()
        res.position.x = x
        res.position.y = y
        res.orientation.z = np.sin(theta / 2)
        res.orientation.w = np.cos(theta / 2)
        return res
    
    def navigate_by_xytheta(self, x, y, theta, blocking = True):
        self.wait_for_server()
        
        goal = MoveBaseGoal()
        goal.target_pose.header.frame_id = 'map'
        goal.target_pose.header.stamp = rospy.Time.now()
        goal.target_pose.pose = self.xytheta_to_pose(x, y, theta)
        self.send_goal(goal)
        
        # rospy.loginfo(f"Navigating to ({x}, {y}, {theta}) ...")
        if blocking:
            self.wait_for_result()
            return self.get_result()
        else:
            return None


class MapFileManager:
    def __init__(self, yaml_path, pgm_path = None):
        with open(yaml_path, 'r') as file:
            # load .yaml file
            data = yaml.safe_load(file)
            self.pgm_path_from_yaml = data['image']
            if pgm_path is None:
                pgm_path = self.pgm_path_from_yaml
            self.resolution = data['resolution']
            self.origin = data['origin']
            
            # load .pgm file (with comment line in line 2)
            with open(pgm_path, 'rb') as pgm_file:
                pgm_header = pgm_file.readline().decode('utf-8').strip() # header
                if pgm_header != 'P5':
                    raise ValueError('Only support P5 PGM file format')
                pgm_file.readline() # comment line
                self.width, self.height = map(int, pgm_file.readline().decode('utf-8').strip().split()) # dimensions
                self.max_value = int(pgm_file.readline().decode('utf-8').strip()) # max value
                self.map = np.fromfile(pgm_file, dtype = np.uint8).reshape((self.height, self.width)) # data
            
            # calculate distance field
            self.distances = distance_transform_cdt(self.map != 0) * self.resolution
            self.distances[0, 0] = 1000.0 # TODO: temporary, for out-of-bound indices
    
    def get_min_distances_from(self, xs, ys):
        x_idx = np.array([int((x - self.origin[0]) / self.resolution) for x in xs])
        y_idx = np.array([self.height - int((y - self.origin[1]) / self.resolution) for y in ys])
        x_idx[(x_idx < 0) | (x_idx >= self.width)] = 0
        y_idx[(y_idx < 0) | (y_idx >= self.height)] = 0
        return self.distances[y_idx, x_idx]


class PolesManager:
    def __init__(self):
        self.visited = []
        self.recognize_tolerance = 0.5
        self.infinte_distance = 1000.0
        self.distance_from_wall_threshold = 0.25
        
        self.poles_pub = rospy.Publisher('/poles', Odometry, queue_size = 10)
        
        self.map_file_manager = MapFileManager('./src/lab7/data/map.yaml', './src/lab7/data/map.pgm')
    
    def publish_pole(self, pole): # tuple(float, float)
        pole_odom = Odometry()
        pole_odom.header.frame_id = 'map'
        pole_odom.header.stamp = rospy.Time.now()
        pole_odom.pose.pose.position.x = pole[0]
        pole_odom.pose.pose.position.y = pole[1]
        self.poles_pub.publish(pole_odom)
    
    def is_same(self, pole1, pole2): # tuple(float, float), tuple(float, float) -> bool
        return np.linalg.norm(np.array(pole1) - np.array(pole2)) < self.recognize_tolerance
    
    def is_visited(self, pole): # tuple(float, float) -> bool
        for visited_pole in self.visited:
            if self.is_same(pole, visited_pole):
                return True
        return False
    
    def wait_for_lidar_ranges(self):
        def circular_median_filter(values, window_radius):
            return np.median([np.roll(values, x) for x in range(1 - window_radius, window_radius)], axis = 0).tolist()
        
        ranges = np.array(rospy.wait_for_message('/scan', LaserScan).ranges)
        ranges_filtered = np.array(circular_median_filter(ranges, 2)) # ranges
        ranges_filtered[ranges_filtered < 0.1] = self.infinte_distance
        return ranges_filtered
    
    def detect(self, robot_x, robot_y, robot_theta):
        ranges = self.wait_for_lidar_ranges()
        
        # remove points near wall
        thetas = np.hstack([np.arange(0, 180), np.arange(-180, 0)]) / 180.0 * np.pi
        xs = robot_x + ranges * np.cos(robot_theta + thetas)
        ys = robot_y + ranges * np.sin(robot_theta + thetas)
        dists_from_wall = self.map_file_manager.get_min_distances_from(xs, ys)
        ranges[dists_from_wall < self.distance_from_wall_threshold] = self.infinte_distance
        
        # return the nearest among left points
        res_idx = np.argmin(ranges)
        if ranges[res_idx] == self.infinte_distance:
            return None, None
        return (xs[res_idx], ys[res_idx]), (ranges[res_idx], thetas[res_idx])


if __name__ == '__main__':
    try:
        rospy.init_node('visit')
        
        client = MyMovebaseClient((3.114527131479146, 1.9957134028056018, np.deg2rad(176))) # P1
        # P3: (-1.207153081893921, -1.7155438661575317, -0.035394341186713855, 0.9993734240072419)
        
        pole_manager = PolesManager()
        
        goals = [
            (1.892370461567939, 1.3399054878040552, np.deg2rad(180)), # P1-2, in, y+0.1
            (-0.9659202038331747, 2.164093551514219, np.deg2rad(180)), # P2, x-0.1, y-0.1
            (-0.9659202038331747, 2.164093551514219, np.deg2rad(-90)), # P2, x-0.1, y-0.1
            (0.8941441783576553, -0.9154355192352538, np.deg2rad(-90)), # P12-34
            (-1.1849005393302707, -1.7624668049043297, np.deg2rad(180)), # P3, x-0.08, y-0.05
            (-1.1849005393302707, -1.7624668049043297, np.deg2rad(0)), # P3, x-0.08, y-0.05
            (2.899912171347615, -2.0176318836143958, np.deg2rad(0)), # P4, y-0.05
            (2.899912171347615, -2.0176318836143958, np.deg2rad(180)), # P4, y-0.05
            (1.3058982355526936, 1.3136307594119567, 0), # P1-2, out
            (3.114527131479146, 2.0557134028056018, np.deg2rad(176)), # P1, y+0.06, TODO
        ]
        
        for goal in goals:
            client.navigate_by_xytheta(*goal, blocking = False)
            
            pole_detected_acc = 0
            pole_detected = None
            while not rospy.is_shutdown() and not client.wait_for_result(rospy.Duration(0.03)): # not rospy.is_shutdown() for SIGINT (?)
                pole_peek, pole_peek_rtheta = pole_manager.detect(*client.get_robot_xytheta())
                if pole_peek is None or pole_peek_rtheta is None:
                    continue # TODO: reset acc?
                
                if pole_peek is not None and not pole_manager.is_visited(pole_peek):
                    if pole_detected is None:
                        pole_detected = pole_peek
                        pole_detected_acc = 1
                    else:
                        if pole_manager.is_same(pole_peek, pole_detected):
                            pole_detected_acc += 1
                        else:
                            pole_detected = pole_peek
                            pole_detected_acc = 1
                else:
                    pole_detected = None
                    pole_detected_acc = 0
                
                if pole_detected_acc >= 10:
                    rospy.loginfo(f"Detected pole at {pole_detected}!")
                    pole_manager.publish_pole(pole_detected)
                    
                    # navigate to pole_detected
                    client.cancel_goal()
                    pole_peek_rtheta
                    
                    # add pole_detected to visited
                    pole_manager.visited.append(pole_detected)
                    
                    # resume navigation
                    client.navigate_by_xytheta(*goal, blocking = False)
                    continue
    
    except rospy.ROSInterruptException:
        rospy.loginfo('ROSInterruptException')
    except KeyboardInterrupt:
        rospy.loginfo('KeyboardInterrupt')

