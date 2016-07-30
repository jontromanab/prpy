#!/usr/bin/python
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import atexit
import math

import numpy
import openravepy
import yaml
from or_trajopt import TrajoptPlanner
import prpy.planning
import prpy.serialization
import prpy_lemur.lemur
import prpy_lemur.roadmaps

from prpy.planning import (
    FirstSupported,
    Sequence,
    PlanningError,
    UnsupportedPlanningError,
    TSRPlanner,
    NamedPlanner,
    CBiRRTPlanner,
    CHOMPPlanner,
    GreedyIKPlanner,
    IKPlanner,
    SnapPlanner,
    VectorFieldPlanner
)

import time 


import os.path
from os.path import join, isfile
from os import listdir
import re

def get_filename(logfile, planner, method, outputdir, trial, seed=None):

    logfile = filter(lambda x: 'log-' in x, re.split(r'[/]|\.yaml',logfile))[0]
    savedir = os.path.join(outputdir, method, logfile, planner)
    if not os.path.exists(savedir):
        os.makedirs(savedir)

    filename = '_'.join(str(x) for x in ['replay', logfile, method, planner, 'trial', trial])
    if seed is not None:
        filename += '_seed_'+str(seed)
    filename += '.yaml'

    completePath = os.path.join(savedir, filename)
    return completePath


parser = argparse.ArgumentParser(description='replay planning request log file')
parser.add_argument('--logfile', required=True)
parser.add_argument('--planner', required=True, help=('cbirrt OMPL_RRTConnect snap chomp vectorfield' 
                                                        ' greedy-ik trajopt lemur cachedlemur combined'))
parser.add_argument('--outdir', default='', type=str, help='Save log to outdir')
parser.add_argument('--collision-checker', choices=['ode','fcl'], default='fcl',
                    help='Set collision checker')

args = parser.parse_args()

planner_list = args.planner.lower().split(' ')
randomized = [x.lower() for x in ['cbirrt', 'OMPL RRTConnect', 'CachedLemur', 'Lemur']]

planners = []
for pl in planner_list: 
    print (pl)
    if pl == 'cbirrt':
        planners.append(CBiRRTPlanner())
    elif pl == 'ompl_rrtconnect':
        planners.append(prpy.planning.ompl.OMPLPlanner('RRTConnect'))
    elif pl == "snap":
        planners.append(SnapPlanner())
    elif pl == 'chomp':
        planners.append(CHOMPPlanner())
    elif pl == 'vectorfield':
        planners.append(VectorFieldPlanner())
    elif pl == 'greedy-ik':
        planners.append(GreedyIKPlanner())
    elif pl == 'trajopt':
        planners.append(TrajoptPlanner())
    elif pl == 'cachedlemur':
        for i in range(10):
            planner = prpy_lemur.lemur.LEMURPlanner(
                roadmap=prpy_lemur.roadmaps.CachedHaltonOffDens(
                    is_cache_required=True, num_per_batch=10000,
                    gamma_factor=1.0, scaling='loglog_n', seed=i))
            setattr(planner, 'seed', i)
            setattr(planner, 'name', 'CachedLemur')
            planners.append(planner)
    elif pl == 'lemur':
        for i in range(10):
            planner = prpy_lemur.lemur.LEMURPlanner(
                roadmap=prpy_lemur.roadmaps.HaltonOffDens(
                    num_per_batch=10000, gamma_factor=1.0, scaling='loglog_n', seed=i))
            setattr(planner, 'seed', i)
            setattr(planner, 'name', 'Lemur')
            planners.append(planner)
    elif pl.lower() == 'combined':
        actual_planner = Sequence(
            SnapPlanner(),
            VectorFieldPlanner(),
            TrajoptPlanner()
        )
        cbirrt_planner = CBiRRTPlanner()

        planner = FirstSupported(
            Sequence(actual_planner,
                     TSRPlanner(delegate_planner=actual_planner),
                     cbirrt_planner),
            NamedPlanner(delegate_planner=Sequence(
                actual_planner,
                cbirrt_planner)))

        setattr(planner,'name','combined')
        planners.append(planner)

    else:
      raise ValueError("Unrecognized planner")

logfile = args.logfile
print ("Reading ", logfile)
start_logfile_at = time.time()

yamldict = yaml.safe_load(open(logfile))
method_name = yamldict['request']['method']


# deserialize environment
import herbpy
env, robot = herbpy.initialize(sim=True)
if args.collision_checker == 'fcl':
    cc = openravepy.RaveCreateCollisionChecker(env, 'fcl')
    assert cc is not None
    env.SetCollisionChecker(openravepy.RaveCreateCollisionChecker(env, 'fcl'))
    env.GetCollisionChecker().SetDescription('fcl')
else:
    cc = openravepy.RaveCreateCollisionChecker(env, 'ode')
    assert cc is not None
    env.SetCollisionChecker(openravepy.RaveCreateCollisionChecker(env, 'ode'))
    env.GetCollisionChecker().SetDescription('ode')

for actual_planner in planners:

    if getattr(actual_planner, 'name', None) == 'combined':
        planner = actual_planner
    else:
        planner = FirstSupported(actual_planner,
                    TSRPlanner(delegate_planner=actual_planner),
                    NamedPlanner(delegate_planner=actual_planner))

    # load planning request
    try:
        method = getattr(planner, yamldict['request']['method'])
    except (AttributeError, UnsupportedPlanningError) as e:
        print ('{} does not support planning method {}!'.format(planner, yamldict['request']['method']))
        continue
    
    num_trials = 3 # if 'plantotsr' in str(method_name).lower() else 3
    for j in range(num_trials):
        
        # deserialize environment
        prpy.serialization.deserialize_environment(yamldict['environment'], env=env, reuse_bodies=[robot])
        
        method_args = []
        for method_arg in yamldict['request']['args']:
            method_args.append(prpy.serialization.deserialize(env, method_arg))
        method_kwargs = {}
        for key,value in yamldict['request']['kw_args'].items():
            method_kwargs[key] = prpy.serialization.deserialize(env, value)

        # remove robot and use properly deserialized robot 
        if robot in method_args:
            method_args.remove(robot)
        if 'robot' in method_kwargs:
            method_kwargs.pop('robot')
        if 'ranker' in method_kwargs:
            method_kwargs.pop('ranker')

        # load ik solver for robot in case it's needed
        ikmodel = openravepy.databases.inversekinematics.InverseKinematicsModel(robot,
            iktype=openravepy.IkParameterizationType.Transform6D)
        if not ikmodel.load():
            ikmodel.autogenerate()

        # call planning method itself ...
        print('calling planning method {} ...'.format(actual_planner))
        from prpy.util import Timer, SetTrajectoryTags
        from prpy.planning.base import Tags

        error_msg = None
        traj = None
        print (actual_planner, 'trial ', j, ' seed ',  getattr(actual_planner, 'seed', None))
        
        start_time = time.time()
        try:
            with env:
                traj = method(robot, *method_args, **method_kwargs)  
        except (UnsupportedPlanningError, AttributeError) as e: 
            import sys
            print (e)
            sys.exit(0)
        except PlanningError as e: 
            error_msg = str(e)
            print (error_msg)
        finally:
            planning_time = time.time() - start_time

        reqdict = {}
        resdict = {}
        reqdict['collisionchecker'] = env.GetCollisionChecker().GetDescription()
        reqdict['args'] = yamldict['request']['args']
        reqdict['kw_args'] = yamldict['request']['kw_args']
        reqdict['method'] = yamldict['request']['method'] 
        reqdict['seed'] = getattr(actual_planner, 'seed', None)
        reqdict['planner_name'] = getattr(actual_planner, 'name', str(planner))
        resdict['ok'] = True if traj else False
        resdict['planning_time'] = planning_time
        if traj is not None: 
            from prpy.util import GetTrajectoryTags
            from prpy.planning.base import Tags
            tags = GetTrajectoryTags(traj)
            resdict['planner_used'] = tags.get(Tags.PLANNER, 'None')
        if error_msg is not None:
            resdict['error'] = str(error_msg)
            
        yamldict_res = {}
        yamldict_res['environment'] = yamldict['environment']
        yamldict_res['request'] = reqdict
        yamldict_res['result'] = resdict
        ok = True if traj else False

        filename = get_filename(logfile, getattr(actual_planner, 'name', str(actual_planner)), method_name, 
                                args.outdir, j, getattr(actual_planner, 'seed', None))
        with open(filename,'w') as fp:
            yaml.safe_dump(yamldict_res, fp)
            print ('\n{} written\n'.format(filename))

        print ("Planning time: ", planning_time)
        
        name = getattr(actual_planner, 'name', str(actual_planner))
        with open('replay-completed-'+name+'.log', 'a') as fp:
            
            trial_info = ' '.join(str(x) for x in [logfile, name, method_name, 'trial', j, ok])
            if getattr(actual_planner, 'seed', None) is not None:
                trial_info += ' seed ' + str(getattr(actual_planner, 'seed', None))
            trial_info += ' ' + str(planning_time)
            fp.write(trial_info+"\n")

logfile_duration = time.time() - start_logfile_at

print ("Took", logfile_duration, "s to finish ", logfile )
