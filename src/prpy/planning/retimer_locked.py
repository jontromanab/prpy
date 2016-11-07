#!/usr/bin/env python

# Copyright (c) 2013, Carnegie Mellon University
# All rights reserved.
# Authors: Michael Koval <mkoval@cs.cmu.edu>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# - Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# - Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# - Neither the name of Carnegie Mellon University nor the names of its
#   contributors may be used to endorse or promote products derived from this
#   software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import logging
import openravepy
from copy import deepcopy
from ..util import (CreatePlannerParametersString, CopyTrajectory,
                    SimplifyTrajectory, HasAffineDOFs, IsTimedTrajectory)
from base import (BasePlanner, PlanningError, ClonedPlanningMethod,
                  LockedPlanningMethod, UnsupportedPlanningError)
from openravepy import PlannerStatus, Planner
from base import Planner as P

logger = logging.getLogger(__name__)

class OpenRAVERetimer_locked(P):
    def __init__(self, algorithm, default_options=None):
        from .base import UnsupportedPlanningError
        from openravepy import RaveCreatePlanner

        super(OpenRAVERetimer_locked, self).__init__()

        self.algorithm = algorithm
        self.default_options = default_options or dict()

    def __str__(self):
        return self.algorithm

    @LockedPlanningMethod
    def RetimeTrajectory(self, robot, path, options=None, **kw_args):
        from openravepy import CollisionOptions, CollisionOptionsStateSaver
        from openravepy import RaveCreatePlanner
        from copy import deepcopy

        env = robot.GetEnv()
        self.planner = RaveCreatePlanner(env, self.algorithm)
        if self.planner is None:
            raise UnsupportedPlanningError(
                'Unable to create "{:s}" planner.'.format(self.algorithm))

        # Validate the input path.
        cspec = path.GetConfigurationSpecification()
        joint_values_group = cspec.GetGroupFromName('joint_values')

        if joint_values_group is None:
            raise ValueError('Trajectory is missing the "joint_values" group.')
        elif HasAffineDOFs(cspec):
            raise UnsupportedPlanningError(
                'OpenRAVERetimer does not support affine DOFs.')
        elif joint_values_group.interpolation != 'linear':
            logger.warning(
                'Path has interpolation of type "%s"; only "linear"'
                ' interpolation is supported.',
                joint_values_group.interpolation)

        # Set parameters.
        all_options = deepcopy(self.default_options)
        if options is not None:
            all_options.update(options)

        params = Planner.PlannerParameters()
        params.SetConfigurationSpecification(
            env, cspec.GetTimeDerivativeSpecification(0))

        params_str = CreatePlannerParametersString(all_options, params)

        # Copy the input trajectory into the planning environment. This is
        # necessary for two reasons: (1) the input trajectory may be in another
        # environment and/or (2) the retimer modifies the trajectory in-place.
        output_traj = CopyTrajectory(path, env=env)

        # Remove co-linear waypoints. Some of the default OpenRAVE retimers do
        # not perform this check internally (e.g. ParabolicTrajectoryRetimer).
        if not IsTimedTrajectory(output_traj):
            output_traj = SimplifyTrajectory(output_traj, robot)

        # Only collision check the active DOFs.
        dof_indices, _ = cspec.ExtractUsedIndices(robot)
        robot.SetActiveDOFs(dof_indices)

        # Compute the timing. This happens in-place.
        self.planner.InitPlan(None, params_str)

        with CollisionOptionsStateSaver(env.GetCollisionChecker(),
                                        CollisionOptions.ActiveDOFs):
            status = self.planner.PlanPath(output_traj, releasegil=True)

        if status not in [PlannerStatus.HasSolution,
                          PlannerStatus.InterruptedWithSolution]:
            raise PlanningError(
                'Retimer returned with status {:s}.'.format(str(status)))

        return output_traj

class HauserParabolicSmoother_locked(OpenRAVERetimer_locked):
    def __init__(self, do_blend=True, do_shortcut=True, blend_radius=0.5,
                 blend_iterations=0, timelimit=3., **kwargs):
        super(HauserParabolicSmoother_locked, self).__init__(
                'HauserParabolicSmoother', **kwargs)

        self.default_options.update({
            'do_blend': int(do_blend),
            'do_shortcut': int(do_shortcut),
            'blend_radius': float(blend_radius),
            'blend_iterations': int(blend_iterations),
            'time_limit': float(timelimit),
        })

    @LockedPlanningMethod
    def RetimeTrajectory_locked(self, robot, path, options=None, **kw_args):
        new_options = deepcopy(options) if options else dict()
        if 'timelimit' in kw_args:
          new_options['time_limit'] = kw_args['timelimit']
        return super(HauserParabolicSmoother_locked, self).RetimeTrajectory(
            robot, path, options=new_options, **kw_args)
