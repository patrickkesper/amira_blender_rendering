#!/usr/bin/env python

# Copyright (c) 2020 - for information on the respective copyright owner
# see the NOTICE file and/or the repository
# <https://github.com/boschresearch/amira-blender-rendering>.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This module specifies a render manager that takes care of several
intermediate steps."""

import bpy
from mathutils import Vector

import os
import numpy as np
import imageio

try:
    import ujson as json
except ModuleNotFoundError:
    import json

import amira_blender_rendering.utils.camera as camera_utils
import amira_blender_rendering.utils.blender as blnd
import amira_blender_rendering.nodes as abr_nodes
import amira_blender_rendering.scenes as abr_scenes
import amira_blender_rendering.math.geometry as abr_geom
from amira_blender_rendering.math.conversions import bu_to_mm

# import things from AMIRA Perception Subsystem that are required
from amira_blender_rendering.interfaces import PoseRenderResult, ResultsCollection
from amira_blender_rendering.postprocessing import boundingbox_from_mask
from amira_blender_rendering.utils.logging import get_logger
# from amira_blender_rendering.utils.converters import to_PASCAL_VOC

logger = get_logger()


class RenderManager(abr_scenes.BaseSceneManager):
    # NOTE: you must call setup_compositor manually when using this class!

    def __init__(self, unit_conversion=bu_to_mm):
        # this will initialize a BaseSceneManager, which is used for setting
        # environment textures, to reset blender, or to initialize default
        # blender settings
        super(RenderManager, self).__init__()
        self.unit_conversion = unit_conversion

    def postprocess(self, dirinfo, base_filename, camera, objs, zeroing, **kwargs):
        """Postprocessing the scene.

        This step will compute all the data that is relevant for
        PoseRenderResult. This data will then be saved to json. In addition,
        postprocessing will fix the filenames generated by blender.
        
        Args:
            dirinfo(DynamicStruct): struct with directory and path info
            base_filename(str): file name
            camera(bpy.types.Camera): active camera object
            objs(list): list of target objects
            zeroing(np.array): array for zeroing camera rotation
        
        Kwargs Args:
            postprocess_config(Configuration): postprocess specific config.
                See abr/scenes/baseconfiguration and scene configs for specific configuration values.
        """
        # get postprocess specific configs
        postprocess_config = kwargs.get('postprocess_config', abr_scenes.BaseConfiguration().postprocess)
    
        # camera matrix
        K_cam = np.asarray(camera_utils.get_calibration_matrix(bpy.context.scene, camera.data))

        # first we update the view-layer to get the updated values in
        # translation and rotation
        bpy.context.view_layer.update()

        # the compositor postprocessing takes care of fixing file names
        # and saving the masks filename into objs

        self.compositor.postprocess()

        # rectify range map into depth
        # Blender depth maps asare indeed ranges. Here we convert ranges into depth values
        fpath_range = os.path.join(dirinfo.images.range, f'{base_filename}.exr')

        # filenames (ranges are stored as true exr values, depth as 16 bit png)
        if not os.path.exists(dirinfo.images.depth):
            os.mkdir(dirinfo.images.depth)
        fpath_depth = os.path.join(dirinfo.images.depth, f'{base_filename}.png')

        # convert
        camera_utils.project_pinhole_range_to_rectified_depth(
            fpath_range,
            fpath_depth,
            res_x=bpy.context.scene.render.resolution_x,
            res_y=bpy.context.scene.render.resolution_y,
            calibration_matrix=K_cam,
            scale=postprocess_config.depth_scale)

        # NOTE: this assumes the camera(s) for which the disparity is computed
        # is(are) the correct one(s). That is it has the correct baseline according to
        # the rendered scene
        if postprocess_config.compute_disparity:
            # check whether current camera name contains any of the given
            # string for parallel setup
            if any([c for c in postprocess_config.parallel_cameras if c in camera.name]):
                # use precomputed depth if available, otherwise use range map
                dirpath = os.path.join(dirinfo.images.base_path, 'disparity')
                if not os.path.exists(dirpath):
                    os.mkdir(dirpath)
                fpath_disparity = os.path.join(dirpath, f'{base_filename}.png')
                # compute map
                camera_utils.compute_disparity_from_z_info(fpath_depth,
                                                           fpath_disparity,
                                                           baseline_mm=postprocess_config.parallel_cameras_baseline_mm,
                                                           calibration_matrix=K_cam,
                                                           res_x=bpy.context.scene.render.resolution_x,
                                                           res_y=bpy.context.scene.render.resolution_y,
                                                           scale=postprocess_config.depth_scale)

        # compute bounding boxes and save annotations
        results_gl = ResultsCollection()
        results_cv = ResultsCollection()
        for obj in objs:
            render_result_gl, render_result_cv = self.build_render_result(
                obj, camera, zeroing, postprocess_config.visibility_from_mask)
            if obj['visible']:
                results_gl.add_result(render_result_gl)
                results_cv.add_result(render_result_cv)
        # if there's no visible object, add single instance results to have general scene information annotated
        if len(results_gl) == 0:
            results_gl.add_result(render_result_gl)
        if len(results_cv) == 0:
            results_cv.add_result(render_result_cv)
        self.save_annotations(dirinfo, base_filename, results_gl, results_cv)

    def setup_renderer(self, integrator: str, enable_denoising: bool, samples: int, motion_blur: bool):
        """Setup blender CUDA rendering, and specify number of samples per pixel to
        use during rendering. If the setting render_setup.samples is not set in the
        configuration, the function defaults to 128 samples per image.
        """
        blnd.activate_cuda_devices()
        # TODO: this hardcodes cycles, but we want a user to specify this
        bpy.context.scene.render.engine = "CYCLES"

        # determine which path tracer is setup in the blender file
        if integrator == 'BRANCHED_PATH':
            self.logger.info(f"integrator set to branched path tracing")
            bpy.context.scene.cycles.progressive = integrator
            bpy.context.scene.cycles.aa_samples = samples
        else:
            self.logger.info(f"integrator set to path tracing")
            bpy.context.scene.cycles.progressive = integrator
            bpy.context.scene.cycles.samples = samples

        # set motion blur
        bpy.context.scene.render.use_motion_blur = motion_blur

        # setup denoising option
        bpy.context.scene.view_layers[0].cycles.use_denoising = enable_denoising
        self.logger.info(f"Denoising enabled" if enable_denoising else f"Denoising disabled")

    def setup_compositor(self, objs, **kw):
        """Setup output compositor nodes"""
        self.compositor = abr_nodes.CompositorNodesOutputRenderedObjects()

        # setup all path related information in the compositor. Note that path
        # related information can be changed via update_dirinfo
        # TODO: both in amira_perception as well as here we actually only need
        # some schema that defines the layout of the dataset. This should be
        # extracted into an independent schema file. Note that this does not
        # mean to use any xml garbage! Rather, it should be as plain as
        # possible.
        self.compositor.setup_nodes(objs, scene=bpy.context.scene, **kw)

    def render(self):
        bpy.ops.render.render(write_still=False)

    def setup_pathspec(self, dirinfo, render_filename: str, objs):
        self.compositor.setup_pathspec(dirinfo, render_filename, objs)

    def convert_units(self, render_result):
        """Convert render_result units from blender units to target unit"""
        if self.unit_conversion is None:
            return render_result

        # convert all relevant units from blender units to target units
        result = render_result
        result.t = self.unit_conversion(result.t)
        result.t_cam = self.unit_conversion(result.t_cam)
        result.aabb = self.unit_conversion(result.aabb)
        result.oobb = self.unit_conversion(result.oobb)

        return result

    def build_render_result(self, obj, camera, zeroing, visibility_from_mask: bool = False):
        """Create render result.

        Args:
            obj(dict): object dictionary to operate on
            camera: blender camera object

        Opt Args:
            visibility_from_mask(bool): if True, if mask is found empty even if object
                            is visible, visibility info are overwritten and
                            set to false

        Returns:
            PoseRenderResult
            """

        # create a pose render result. leave image fields empty, they will
        # currenlty not go to the state dict. this is only here to make sure
        # that we actually get the state dict defined in pose render result
        t = np.asarray(abr_geom.get_relative_translation(obj['bpy'], camera))
        R = np.asarray(abr_geom.get_relative_rotation_to_cam_deg(obj['bpy'], camera, Vector(zeroing)).to_matrix())

        # camera world coordinate transformation
        t_cam = np.asarray(camera.matrix_world.to_translation())
        R_cam = np.asarray(camera.matrix_world.to_3x3().normalized())

        # compute bounding boxes
        corners2d, corners3d, aabb, oobb = None, None, None, None
        if obj['visible']:
            # this rises a ValueError if mask info is not correct
            corners2d = self.compute_2dbbox(obj['fname_mask'])
            if corners2d is not None:
                aabb, oobb, corners3d = self.compute_3dbbox(obj['bpy'])
            elif visibility_from_mask:
                logger.warn(f'Given mask found empty. '
                            f'Overwriting visibility information for obj {obj["object_class_name"]}:{obj["object_id"]}')
                obj['visible'] = False
            else:
                raise ValueError('Invalid mask given')

        render_result_gl = PoseRenderResult(
            object_class_name=obj['object_class_name'],
            object_class_id=obj['object_class_id'],
            object_name=obj['bpy'].name,
            object_id=obj['object_id'],
            rgb_const=None,
            rgb_random=None,
            depth=None,
            mask=None,
            rotation=R,
            translation=t,
            corners2d=corners2d,
            corners3d=corners3d,
            aabb=aabb,
            oobb=oobb,
            mask_name=obj['id_mask'],
            visible=obj['visible'],
            camera_rotation=R_cam,
            camera_translation=t_cam)

        # build results in OpenCV format
        R_cv, t_cv = abr_geom.gl2cv(R, t)

        # for the camera we only need to update the rotation. That is because in OpenCV
        # format it is assumed the camera looks towards positive z (rotation of pi around x)
        # Thus to express the rotation in world coordinate we post-multiply the rotation matrix.
        # However, its position/location wrt to the world coordinate system does not change.
        R_cam_cv = R_cam.dot(abr_geom.euler_x_to_matrix(np.pi))
        t_cam_cv = t_cam

        render_result_cv = PoseRenderResult(
            object_class_name=obj['object_class_name'],
            object_class_id=obj['object_class_id'],
            object_name=obj['bpy'].name,
            object_id=obj['object_id'],
            rgb_const=None,
            rgb_random=None,
            depth=None,
            mask=None,
            rotation=R_cv,
            translation=t_cv,
            corners2d=corners2d,
            corners3d=corners3d,
            aabb=aabb,
            oobb=oobb,
            mask_name=obj['id_mask'],
            visible=obj['visible'],
            camera_rotation=R_cam_cv,
            camera_translation=t_cam_cv)

        # convert to desired units
        render_result_gl = self.convert_units(render_result_gl)
        render_result_cv = self.convert_units(render_result_cv)
        return render_result_gl, render_result_cv

    def save_annotations(self, dirinfo, base_filename, results_gl: ResultsCollection, results_cv: ResultsCollection):
        """
        Save annotations of Render Results given in ResultsCollection

        Args:
            results_gl(ResultsCollection): collection of <PoseRenderResult> in OpenGL convetion
            results_cv(ResultsCollection): collection of <PoseRenderResult> in OpenCV convetion
        """
        # check if directory structure is already there
        for k in dirinfo.annotations:
            if not os.path.exists(dirinfo.annotations[k]):
                os.makedirs(dirinfo.annotations[k], exist_ok=True)  # create entire tree if necessary

        # first dump to json opengl data
        fname_json = f"{base_filename}.json"
        fpath_json = os.path.join(dirinfo.annotations.opengl, f"{fname_json}")
        json_data = results_gl.state_dict()
        with open(fpath_json, 'w') as f:
            json.dump(json_data, f, indent=0)

        # second dump to json opencv data
        fpath_json = os.path.join(dirinfo.annotations.opencv, f'{fname_json}')
        json_data = results_cv.state_dict()
        with open(fpath_json, 'w') as f:
            json.dump(json_data, f, indent=0)

        # create xml annotation files according to PASCAL VOC format
        # TODO: this should be an option or convert afterwards. Not everyone wants to convert to PASCAL_VOC
        # to_PASCAL_VOC(fpath_json)

    def compute_2dbbox(self, fname_mask):
        """Compute the 2D bounding box around an object given the mask filename

        This simply loads the file from disk and gets the pixels. Unfortunately,
        it is not possible right now to work around this with using blender's
        viewer nodes. That is, using a viewer node attached to ID Mask nodes
        will store an image only to bpy.data.Images['Viewer Node'], depending on
        which node is currently selected in the node editor... I have yet to find a
        programmatic way that circumvents re-loading the file from disk

        Args:
            fname_mask(str): mask filename

        Raises:
            ValueError if an empty mask is given
        """
        # this is a HxWx3 tensor (RGBA or RGB data)
        mask = imageio.imread(fname_mask)
        mask = np.sum(mask, axis=2)
        return boundingbox_from_mask(mask)

    def reorder_bbox(self, aabb, order=[1, 0, 2, 3, 5, 4, 6, 7]):
        """Reorder the vertices in an aab according to a certain permutation order."""

        if len(aabb) != 8:
            raise RuntimeError(f'Unexpected length of aabb (is {len(aabb)}, should be 8)')

        result = list()
        for i in range(8):
            result.append(aabb[order[i]])

        return result

    def compute_3dbbox(self, obj: bpy.types.Object):
        """Compute all 3D bounding boxes (axis aligned, object oriented, and the 3D corners

        Blender has the coordinates and bounding box in the following way.

        The world coordinate system has x pointing right, y pointing forward,
        z pointing upwards. Then, indexing with x/y/z, the bounding box
        corners are taken from the following axes:

          0:  -x/-y/-z
          1:  -x/-y/+z
          2:  -x/+y/+z
          3:  -x/+y/-z
          4:  +x/-y/-z
          5:  +x/-y/+z
          6:  +x/+y/+z
          7:  +x/+y/-z

        This differs from the order of the bounding box as it was used in
        OpenGL. Ignoring the first item (centroid), the following re-indexing is
        required to get it into the correct order: [1, 0, 2, 3, 5, 4, 6, 7].
        This will be done after getting the aabb from blender, using function
        reorder_bbox.

        TODO: probably, using numpy is not at all required, we could directly
              store to lists. have to decide if we want this or not
        """

        # 0. storage for numpy arrays.
        np_aabb = np.zeros((9, 3))
        np_oobb = np.zeros((9, 3))
        np_corners3d = np.zeros((9, 2))

        # 1. get centroid and bounding box of object in world coordinates by
        # applying the objects rotation matrix to the bounding box of the object

        # axis aligned (no object rotation)
        aabb = [Vector(v) for v in obj.bound_box]
        # compute centroid
        aa_centroid = aabb[0] + (aabb[6] - aabb[0]) / 2.0
        # copy aabb before reordering to have access to it later
        aabb_orig = aabb
        # fix order of aabb for RenderedObjects
        aabb = self.reorder_bbox(aabb)
        # convert to numpy
        np_aabb[0, :] = np.array((aa_centroid[0], aa_centroid[1], aa_centroid[2]))
        for i in range(8):
            np_aabb[i + 1, :] = np.array((aabb[i][0], aabb[i][1], aabb[i][2]))

        # object aligned (that is, including object rotation)
        oobb = [obj.matrix_world @ v for v in aabb_orig]
        # compute oo centroid
        oo_centroid = oobb[0] + (oobb[6] - oobb[0]) / 2.0
        # fix order for rendered objects
        oobb = self.reorder_bbox(oobb)

        # convert to numpy
        np_oobb[0, :] = np.array((oo_centroid[0], oo_centroid[1], oo_centroid[2]))
        for i in range(8):
            np_oobb[i + 1, :] = np.array((oobb[i][0], oobb[i][1], oobb[i][2]))

        # project centroid+vertices and convert to pixel coordinates
        corners3d = []
        prj = abr_geom.project_p3d(oo_centroid, bpy.context.scene.camera)
        pix = abr_geom.p2d_to_pixel_coords(prj)
        corners3d.append(pix)
        np_corners3d[0, :] = np.array((corners3d[-1][0], corners3d[-1][1]))

        for i, v in enumerate(oobb):
            prj = abr_geom.project_p3d(v, bpy.context.scene.camera)
            pix = abr_geom.p2d_to_pixel_coords(prj)
            corners3d.append(pix)
            np_corners3d[i + 1, :] = np.array((corners3d[-1][0], corners3d[-1][1]))

        return np_aabb, np_oobb, np_corners3d
