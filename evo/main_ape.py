#!/usr/bin/env python
# -*- coding: UTF8 -*-
# PYTHON_ARGCOMPLETE_OK
"""
Main executable for calculating the absolute pose error (APE) metric.
author: Michael Grupp

This file is part of evo (github.com/MichaelGrupp/evo).

evo is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

evo is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with evo.  If not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import logging

import numpy as np

import evo.common_ape_rpe as common
from evo.core import lie_algebra, sync, metrics
from evo.core.result import Result
from evo.core.trajectory import PosePath3D, PoseTrajectory3D
from evo.tools import file_interface, log
from evo.tools.settings import SETTINGS

logger = logging.getLogger(__name__)

SEP = "-" * 80  # separator line


def parser() -> argparse.ArgumentParser:
    basic_desc = "Absolute pose error (APE) metric app"
    lic = "(c) evo authors"

    shared_parser = argparse.ArgumentParser(add_help=False)
    algo_opts = shared_parser.add_argument_group("algorithm options")
    output_opts = shared_parser.add_argument_group("output options")
    usability_opts = shared_parser.add_argument_group("usability options")

    algo_opts.add_argument(
        "-r", "--pose_relation", default="trans_part",
        help="pose relation on which the APE is based", choices=[
            "full", "trans_part", "rot_part", "angle_deg", "angle_rad",
            "point_distance"
        ])
    algo_opts.add_argument("-a", "--align",
                           help="alignment with Umeyama's method (no scale)",
                           action="store_true")
    algo_opts.add_argument("-s", "--correct_scale", action="store_true",
                           help="correct scale with Umeyama's method")
    algo_opts.add_argument(
        "--n_to_align",
        help="the number of poses to use for Umeyama alignment, "
        "counted from the start (default: all)", default=-1, type=int)
    algo_opts.add_argument(
        "--align_origin",
        help="align the trajectory origin to the origin of the reference "
        "trajectory", action="store_true")

    output_opts.add_argument(
        "-p",
        "--plot",
        action="store_true",
        help="show plot window",
    )
    output_opts.add_argument(
        "--plot_mode", default=SETTINGS.plot_mode_default,
        help="the axes for plot projection",
        choices=["xy", "xz", "yx", "yz", "zx", "zy", "xyz"])
    output_opts.add_argument(
        "--plot_x_dimension", choices=["index", "seconds",
                                       "distances"], default="seconds",
        help="dimension that is used on the x-axis of the raw value plot"
        "(default: seconds, or index if no timestamps are present)")
    output_opts.add_argument(
        "--plot_colormap_max", type=float,
        help="the upper bound used for the color map plot "
        "(default: maximum error value)")
    output_opts.add_argument(
        "--plot_colormap_min", type=float,
        help="the lower bound used for the color map plot "
        "(default: minimum error value)")
    output_opts.add_argument(
        "--plot_colormap_max_percentile", type=float,
        help="percentile of the error distribution to be used "
        "as the upper bound of the color map plot "
        "(in %%, overrides --plot_colormap_max)")
    output_opts.add_argument(
        "--plot_full_ref",
        action="store_true",
        help="plot the full, unsynchronized reference trajectory",
    )
    output_opts.add_argument(
        "--ros_map_yaml", help="yaml file of an ROS 2D map image (.pgm/.png)"
        " that will be drawn into the plot", default=None)
    output_opts.add_argument("--save_plot", default=None,
                             help="path to save plot")
    output_opts.add_argument("--serialize_plot", default=None,
                             help="path to serialize plot (experimental)")
    output_opts.add_argument("--save_results",
                             help=".zip file path to store results")
    output_opts.add_argument("--logfile", help="Local logfile path.",
                             default=None)
    usability_opts.add_argument("--no_warnings", action="store_true",
                                help="no warnings requiring user confirmation")
    usability_opts.add_argument("-v", "--verbose", action="store_true",
                                help="verbose output")
    usability_opts.add_argument("--silent", action="store_true",
                                help="don't print any output")
    usability_opts.add_argument(
        "--debug", action="store_true",
        help="verbose output with additional debug info")
    usability_opts.add_argument(
        "-c", "--config",
        help=".json file with parameters (priority over command line args)")

    main_parser = argparse.ArgumentParser(
        description="{} {}".format(basic_desc, lic))
    sub_parsers = main_parser.add_subparsers(dest="subcommand")
    sub_parsers.required = True

    kitti_parser = sub_parsers.add_parser(
        "kitti", parents=[shared_parser],
        description="{} for KITTI pose files - {}".format(basic_desc, lic))
    kitti_parser.add_argument("ref_file",
                              help="reference pose file (ground truth)")
    kitti_parser.add_argument("est_file", help="estimated pose file")

    tum_parser = sub_parsers.add_parser(
        "tum", parents=[shared_parser],
        description="{} for TUM trajectory files - {}".format(basic_desc, lic))
    tum_parser.add_argument("ref_file", help="reference trajectory file")
    tum_parser.add_argument("est_file", help="estimated trajectory file")

    euroc_parser = sub_parsers.add_parser(
        "euroc", parents=[shared_parser],
        description="{} for EuRoC MAV files - {}".format(basic_desc, lic))
    euroc_parser.add_argument(
        "state_gt_csv",
        help="ground truth: <seq>/mav0/state_groundtruth_estimate0/data.csv")
    euroc_parser.add_argument("est_file",
                              help="estimated trajectory file in TUM format")

    bag_parser = sub_parsers.add_parser(
        "bag", parents=[shared_parser],
        description="{} for ROS bag files - {}".format(basic_desc, lic))
    bag_parser.add_argument("bag", help="ROS bag file")
    bag_parser.add_argument("ref_topic", help="reference trajectory topic")
    bag_parser.add_argument("est_topic", help="estimated trajectory topic")

    # Add time-sync options to parser of trajectory formats.
    for trajectory_parser in {bag_parser, euroc_parser, tum_parser}:
        trajectory_parser.add_argument(
            "--t_max_diff", type=float, default=0.01,
            help="maximum timestamp difference for data association")
        trajectory_parser.add_argument(
            "--t_offset", type=float, default=0.0,
            help="constant timestamp offset for data association")
        trajectory_parser.add_argument(
            "--t_start", type=float, default=None,
            help="only use data with timestamps "
            "greater or equal this start time")
        trajectory_parser.add_argument(
            "--t_end", type=float, default=None,
            help="only use data with timestamps less or equal this end time")

    return main_parser


def ape(traj_ref: PosePath3D, traj_est: PosePath3D,
        pose_relation: metrics.PoseRelation, align: bool = False,
        correct_scale: bool = False, n_to_align: int = -1,
        align_origin: bool = False, ref_name: str = "reference",
        est_name: str = "estimate") -> Result:

    # Align the trajectories.
    only_scale = correct_scale and not align
    alignment_transformation = None
    if align or correct_scale:
        logger.debug(SEP)
        alignment_transformation = lie_algebra.sim3(
            *traj_est.align(traj_ref, correct_scale, only_scale, n=n_to_align))
    elif align_origin:
        logger.debug(SEP)
        alignment_transformation = traj_est.align_origin(traj_ref)

    # Calculate APE.
    logger.debug(SEP)
    data = (traj_ref, traj_est)
    ape_metric = metrics.APE(pose_relation)
    ape_metric.process_data(data)

    title = str(ape_metric)
    if align and not correct_scale:
        title += "\n(with SE(3) Umeyama alignment)"
    elif align and correct_scale:
        title += "\n(with Sim(3) Umeyama alignment)"
    elif only_scale:
        title += "\n(scale corrected)"
    elif align_origin:
        title += "\n(with origin alignment)"
    else:
        title += "\n(not aligned)"
    if (align or correct_scale) and n_to_align != -1:
        title += " (aligned poses: {})".format(n_to_align)

    ape_result = ape_metric.get_result(ref_name, est_name)
    ape_result.info["title"] = title

    logger.debug(SEP)
    logger.info(ape_result.pretty_str())

    ape_result.add_trajectory(ref_name, traj_ref)
    ape_result.add_trajectory(est_name, traj_est)
    if isinstance(traj_est, PoseTrajectory3D):
        seconds_from_start = np.array(
            [t - traj_est.timestamps[0] for t in traj_est.timestamps])
        ape_result.add_np_array("seconds_from_start", seconds_from_start)
        ape_result.add_np_array("timestamps", traj_est.timestamps)
        distances_from_start = np.array(
            [d - traj_ref.distances[0] for d in traj_ref.distances])
        ape_result.add_np_array("distances_from_start", distances_from_start)
        ape_result.add_np_array("distances", traj_est.distances)

    if alignment_transformation is not None:
        ape_result.add_np_array("alignment_transformation_sim3",
                                alignment_transformation)

    return ape_result


def run(args: argparse.Namespace) -> None:
    log.configure_logging(args.verbose, args.silent, args.debug,
                          local_logfile=args.logfile)
    if args.debug:
        from pprint import pformat
        parser_str = pformat({arg: getattr(args, arg) for arg in vars(args)})
        logger.debug("main_parser config:\n{}".format(parser_str))
    logger.debug(SEP)

    traj_ref, traj_est, ref_name, est_name = common.load_trajectories(args)

    traj_ref_full = None
    if args.plot_full_ref:
        import copy
        traj_ref_full = copy.deepcopy(traj_ref)

    if isinstance(traj_ref, PoseTrajectory3D) and isinstance(
            traj_est, PoseTrajectory3D):
        logger.debug(SEP)
        if args.t_start or args.t_end:
            if args.t_start:
                logger.info("Using time range start: {}s".format(args.t_start))
            if args.t_end:
                logger.info("Using time range end: {}s".format(args.t_end))
            traj_ref.reduce_to_time_range(args.t_start, args.t_end)
        logger.debug("Synchronizing trajectories...")
        traj_ref, traj_est = sync.associate_trajectories(
            traj_ref, traj_est, args.t_max_diff, args.t_offset,
            first_name=ref_name, snd_name=est_name)

    pose_relation = common.get_pose_relation(args)

    result = ape(
        traj_ref=traj_ref,
        traj_est=traj_est,
        pose_relation=pose_relation,
        align=args.align,
        correct_scale=args.correct_scale,
        n_to_align=args.n_to_align,
        align_origin=args.align_origin,
        ref_name=ref_name,
        est_name=est_name,
    )

    if args.plot or args.save_plot or args.serialize_plot:
        common.plot_result(args, result, traj_ref,
                           result.trajectories[est_name],
                           traj_ref_full=traj_ref_full)

    if args.save_results:
        logger.debug(SEP)
        if not SETTINGS.save_traj_in_zip:
            del result.trajectories[ref_name]
            del result.trajectories[est_name]
        file_interface.save_res_file(args.save_results, result,
                                     confirm_overwrite=not args.no_warnings)


if __name__ == '__main__':
    from evo import entry_points
    entry_points.ape()
