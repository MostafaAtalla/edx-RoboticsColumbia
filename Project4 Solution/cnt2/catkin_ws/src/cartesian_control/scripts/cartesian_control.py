#!/usr/bin/env python

import math
import numpy
import time
from threading import Thread, Lock

import rospy
import tf
from geometry_msgs.msg import Transform
from sensor_msgs.msg import JointState
from std_msgs.msg import Float32
from urdf_parser_py.urdf import URDF


def S_matrix(w):    # Skew matrix
    S = numpy.zeros((3,3))
    S[0,1] = -w[2]
    S[0,2] =  w[1]
    S[1,0] =  w[2]
    S[1,2] = -w[0]
    S[2,0] = -w[1]
    S[2,1] =  w[0]
    return S


def rotation_matrix(matrix):
    R = numpy.array(matrix, dtype=numpy.float64, copy=False)
    R33 = R[:3, :3]
    return R33


def translation_matrix(matrix):
    R = numpy.array(matrix, dtype=numpy.float64, copy=False)
    #t43 = numpy.zeros((3,1))
    #t43[:3] = R[:3, 3:]
    t43 = R[:3, 3]
    return t43


# This is the function that must be filled in as part of the Project.
def cartesian_control(joint_transforms, b_T_ee_current, b_T_ee_desired,
                      red_control, q_current, q0_desired):
    num_joints = len(joint_transforms)
    dq = numpy.zeros(num_joints)
    #-------------------- Fill in your code here ---------------------------
    p_x=2 #proportional gain for translational velocity
    p_w=2 #proportional gain for rotational velocity
    
    max_vel=0.1       #max translational velocity 
    max_omega=0.1
    
    # Compute the transformation from the end effector current position to the end effector desired position
    ee_current_T_ee_desired=numpy.dot(numpy.linalg.inv(b_T_ee_current),b_T_ee_desired)
    
    # Compute the change in position and oreintations vector
    delta_x=ee_current_T_ee_desired[:3,3]
    
    axis,angle=rotation_from_matrix(ee_current_T_ee_desired[:3,:3])
    
    delta_w=axis*angle
    
    # Compute the velocity vectors and make a cap
    V_x=delta_x*p_x
    
    V_w=delta_w*p_w
    
    
    
    # Compute the jacobian matrix numerically
    J=numpy.zeros((6,num_joints))
    for i in range(num_joints):
        Z_axis=joint_transforms[i][:3, 2]
        t=numpy.subtract(b_T_ee_current[:3, 3],joint_transforms[i][:3, 3])
        #sk_t=numpy.array([[0,-t[2],t[1]],[t[2],0,-t[0]],[-t[1],t[0],0]])
        
        #J[:3,i]=numpy.dot(sk_t,R)[:,2]
        J[:3,i]= numpy.cross(Z_axis,t)
        J[3:6,i]= Z_axis
    
    
    #Transform the V_ee from end-effector frame to base frame
    V_x=numpy.dot(b_T_ee_current[:3,:3],V_x)
    V_w=numpy.dot(b_T_ee_current[:3,:3],V_w)
    
    V_ee=numpy.concatenate((V_x,V_w),axis=None) #expressed in base frame
    
    #Compute joint velocities
    dq=numpy.dot(numpy.linalg.pinv(J,0.01),V_ee)
    
    #----------------------------------------------------------------------
    return dq
    
def convert_from_message(t):
    trans = tf.transformations.translation_matrix((t.translation.x,
                                                  t.translation.y,
                                                  t.translation.z))
    rot = tf.transformations.quaternion_matrix((t.rotation.x,
                                                t.rotation.y,
                                                t.rotation.z,
                                                t.rotation.w))
    T = numpy.dot(trans,rot)
    return T

# Returns the angle-axis representation of the rotation contained in the input matrix
# Use like this:
# angle, axis = rotation_from_matrix(R)
def rotation_from_matrix(matrix):
    R = numpy.array(matrix, dtype=numpy.float64, copy=False)
    R33 = R[:3, :3]
    # axis: unit eigenvector of R33 corresponding to eigenvalue of 1
    l, W = numpy.linalg.eig(R33.T)
    i = numpy.where(abs(numpy.real(l) - 1.0) < 1e-8)[0]
    if not len(i):
        raise ValueError("no unit eigenvector corresponding to eigenvalue 1")
    axis = numpy.real(W[:, i[-1]]).squeeze()
    # point: unit eigenvector of R33 corresponding to eigenvalue of 1
    l, Q = numpy.linalg.eig(R)
    i = numpy.where(abs(numpy.real(l) - 1.0) < 1e-8)[0]
    if not len(i):
        raise ValueError("no unit eigenvector corresponding to eigenvalue 1")
    # rotation angle depending on axis
    cosa = (numpy.trace(R33) - 1.0) / 2.0
    if abs(axis[2]) > 1e-8:
        sina = (R[1, 0] + (cosa-1.0)*axis[0]*axis[1]) / axis[2]
    elif abs(axis[1]) > 1e-8:
        sina = (R[0, 2] + (cosa-1.0)*axis[0]*axis[2]) / axis[1]
    else:
        sina = (R[2, 1] + (cosa-1.0)*axis[1]*axis[2]) / axis[0]
    angle = math.atan2(sina, cosa)
    return angle, axis

class CartesianControl(object):

    #Initialization
    def __init__(self):
        #Loads the robot model, which contains the robot's kinematics information
        self.robot = URDF.from_parameter_server()

        #Subscribes to information about what the current joint values are.
        rospy.Subscriber("/joint_states", JointState, self.joint_callback)

        #Subscribes to command for end-effector pose
        rospy.Subscriber("/cartesian_command", Transform, self.command_callback)

        #Subscribes to command for redundant dof
        rospy.Subscriber("/redundancy_command", Float32, self.redundancy_callback)

        # Publishes desired joint velocities
        self.pub_vel = rospy.Publisher("/joint_velocities", JointState, queue_size=1)

        #This is where we hold the most recent joint transforms
        self.joint_transforms = []
        self.q_current = []
        self.x_current = tf.transformations.identity_matrix()
        self.R_base = tf.transformations.identity_matrix()
        self.x_target = tf.transformations.identity_matrix()
        self.q0_desired = 0
        self.last_command_time = 0
        self.last_red_command_time = 0

        # Initialize timer that will trigger callbacks
        self.mutex = Lock()
        self.timer = rospy.Timer(rospy.Duration(0.1), self.timer_callback)

    def command_callback(self, command):
        self.mutex.acquire()
        self.x_target = convert_from_message(command)
        self.last_command_time = time.time()
        self.mutex.release()

    def redundancy_callback(self, command):
        self.mutex.acquire()
        self.q0_desired = command.data
        self.last_red_command_time = time.time()
        self.mutex.release()        
        
    def timer_callback(self, event):
        msg = JointState()
        self.mutex.acquire()
        if time.time() - self.last_command_time < 0.5:
            dq = cartesian_control(self.joint_transforms, 
                                   self.x_current, self.x_target,
                                   False, self.q_current, self.q0_desired)
            msg.velocity = dq
        elif time.time() - self.last_red_command_time < 0.5:
            dq = cartesian_control(self.joint_transforms, 
                                   self.x_current, self.x_current,
                                   True, self.q_current, self.q0_desired)
            msg.velocity = dq
        else:            
            msg.velocity = numpy.zeros(7)
        self.mutex.release()
        self.pub_vel.publish(msg)
        
    def joint_callback(self, joint_values):
        root = self.robot.get_root()
        T = tf.transformations.identity_matrix()
        self.mutex.acquire()
        self.joint_transforms = []
        self.q_current = joint_values.position
        self.process_link_recursive(root, T, joint_values)
        self.mutex.release()

    def align_with_z(self, axis):
        T = tf.transformations.identity_matrix()
        z = numpy.array([0,0,1])
        x = numpy.array([1,0,0])
        dot = numpy.dot(z,axis)
        if dot == 1: return T
        if dot == -1: return tf.transformation.rotation_matrix(math.pi, x)
        rot_axis = numpy.cross(z, axis)
        angle = math.acos(dot)
        return tf.transformations.rotation_matrix(angle, rot_axis)

    def process_link_recursive(self, link, T, joint_values):
        if link not in self.robot.child_map: 
            self.x_current = T
            return
        for i in range(0,len(self.robot.child_map[link])):
            (joint_name, next_link) = self.robot.child_map[link][i]
            if joint_name not in self.robot.joint_map:
                rospy.logerror("Joint not found in map")
                continue
            current_joint = self.robot.joint_map[joint_name]        

            trans_matrix = tf.transformations.translation_matrix((current_joint.origin.xyz[0], 
                                                                  current_joint.origin.xyz[1],
                                                                  current_joint.origin.xyz[2]))
            rot_matrix = tf.transformations.euler_matrix(current_joint.origin.rpy[0], 
                                                         current_joint.origin.rpy[1],
                                                         current_joint.origin.rpy[2], 'rxyz')
            origin_T = numpy.dot(trans_matrix, rot_matrix)
            current_joint_T = numpy.dot(T, origin_T)
            if current_joint.type != 'fixed':
                if current_joint.name not in joint_values.name:
                    rospy.logerror("Joint not found in list")
                    continue
                # compute transform that aligns rotation axis with z
                aligned_joint_T = numpy.dot(current_joint_T, self.align_with_z(current_joint.axis))
                self.joint_transforms.append(aligned_joint_T)
                index = joint_values.name.index(current_joint.name)
                angle = joint_values.position[index]
                joint_rot_T = tf.transformations.rotation_matrix(angle, 
                                                                 numpy.asarray(current_joint.axis))
                next_link_T = numpy.dot(current_joint_T, joint_rot_T) 
            else:
                next_link_T = current_joint_T

            self.process_link_recursive(next_link, next_link_T, joint_values)
        
if __name__ == '__main__':
    rospy.init_node('cartesian_control', anonymous=True)
    cc = CartesianControl()
    rospy.spin()
