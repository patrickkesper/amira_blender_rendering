#!/usr/bin/env python

"""This script can be used to generate RenderedObjects datasets of the cap tool.

The script must be run in blender, for instance from the command line using:

    $ blender -b -P scripts/render_tool_cap.py

The script accepts several additional arguments. The most important is the path
to the render configuration file, which defaults to 'config/render_toolcap.cfg'.
The script also needs to find amira_blender_rendering, as well as aps (AMIRA
Perception Subsystem) and foundry (also part of amira_deep_vision). Path to
their parent-folders can be passed along as command-line arguments via the
--arb-path and --aps-path flags.s

Example:

    $ blender -b -P scripts/render_tool_cap.py -- --arb-path ~/amira/amira_blender_rendering --aps-path ~/amira/amira_deep_vision

Note that paths will be expanded, i.e. variables such as $AMIRA_DATASETS or ~
will be turned into their proper values.

"""


# make amira_deep_vision packages available
import sys, os
import argparse, configparser
import numpy as np
import random
from math import log, ceil

def expandpath(path):
    return os.path.expandvars(os.path.expanduser(path))


def import_aps(path=None):
    """Import the AMIRA Perception Subsystem."""
    if path is not None:
        sys.path.append(expandpath(path))

    global aps
    global foundry

    import aps
    import aps.core

    import foundry
    import foundry.utils


def import_abr(path=None):
    """Import amira_blender_rendering."""
    if path is not None:
        sys.path.append(expandpath(path))

    global abr
    import amira_blender_rendering as abr
    import amira_blender_rendering.blender_utils
    import amira_blender_rendering.scenes


def import_ro_static(aps_path):
    """Import the static methods from renderedobjects.

    We cannot import aps.data, because blender<->torch has some gflags issues that, at
    the moment, we cannot easily solve. That is, when running

        $ blender -c --python

    in a console, and then trying to import torch in the console

        >>> import torch

    you'll get an ERROR and blender quits. To circumvent this issue, we'll
    manually import the file that gives us directory information of
    renderedobjects within the following function. Fore more information, read
    the comment in the file that gets imported.
    """

    global ro_static

    APS_RENDERED_OBJECTS_STATIC_METHODS = 'aps/data/datasets/renderedobjects_static.py'
    import importlib
    try:
        fname = os.path.expanduser(os.path.join(
            aps_path,
            APS_RENDERED_OBJECTS_STATIC_METHODS))
        spec = importlib.util.spec_from_file_location('renderedobjects_static', fname)
        ro_static = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ro_static)
    except ImportError as e:
        raise RuntimeError(f"Could not import RenderedObjects' static methods")


def get_environment_textures(cfg):
    """Determine if the user wants to set specific environment texture, or
    randomly select from a directory"""

    environment_textures = expandpath(cfg['render_setup']['environment_texture'])
    if os.path.isdir(environment_textures):
        files = os.listdir(environment_textures)
        environment_textures = [os.path.join(environment_textures, f) for f in files]
    else:
        environment_textures = [environment_textures]

    return environment_textures


def generate_dataset(cfg, dirinfo):
    """Generate images and metadata for a dataset, specified by cfg and dirinfo"""

    image_count = int(cfg['dataset']['image_count'])
    environment_textures = get_environment_textures(cfg)

    # filename setup
    format_width = int(ceil(log(image_count, 10)))
    base_filename = "{:0{width}d}".format(0, width=format_width)

    # setup blender CUDA rendering
    abr.blender_utils.activate_cuda_devices()

    # scene setup with a calibrated camera.
    # NOTE: at the moment there is a bug in abr.camera_utils:opencv_to_blender,
    #       which prevents us from actually using a calibrated camera. Still, we
    #       pass it along here because at some point, we might actually have
    #       working implementation ;)
    width  = int(cfg['camera_info']['width'])
    height = int(cfg['camera_info']['height'])
    K = None
    if 'K' in cfg['camera_info']:
        K = np.fromstring(cfg['camera_info']['K'], sep=',')
    scene = abr.scenes.SimpleToolCap(base_filename, dirinfo, K, width, height)

    # generate images
    for i in range(image_count):
        # setup filename
        base_filename = "{:0{width}d}".format(i, width=format_width)
        scene.set_base_filename(base_filename)

        # set some environment texture
        filepath = expandpath(random.choice(environment_textures))
        scene.set_environment_texture(filepath)

        # actual rendering
        scene.randomize()
        scene.render()
        scene.postprocess()


def get_argv():
    """Get argv after --"""
    try:
        # only arguments after --
        return sys.argv[sys.argv.index('--') + 1:]
    except ValueError:
        return []


def main():
    parser = argparse.ArgumentParser(description='Render dataset for the "cap tool"', prog="blender -b -P " + __file__)
    parser.add_argument('--config', default='config/render_toolcap.cfg', help='Path to configuration file')
    parser.add_argument('--aps-path', default='~/dev/vision/amira_deep_vision', help='Path where AMIRA Perception Subsystem (aps) can be found')
    parser.add_argument('--abr-path', default='~/dev/vision/amira_blender_rendering/src', help='Path where amira_blender_rendering can be found')
    args = parser.parse_args(args=get_argv())

    # special imports. will also set system path for abr and aps
    import_aps(args.aps_path)
    import_abr(args.abr_path)
    import_ro_static(args.aps_path)

    # read configuration file
    # TODO: change to Configuration here and in foundry
    config = configparser.ConfigParser()
    config.read(expandpath(args.config))
    config = foundry.utils.check_paths(config)
    cfgs = foundry.utils.build_splitting_configs(config)

    for cfg in cfgs:
        # build directory structure and run rendering
        # TODO: rename all configs from output_dir to output_path
        dirinfo = ro_static.build_directory_info(cfg['dataset']['output_dir'])

        # generate it
        generate_dataset(cfg, dirinfo)

        # save configuration
        foundry.utils.dump_config(cfg, dirinfo.base_path)


if __name__ == "__main__":
    main()

