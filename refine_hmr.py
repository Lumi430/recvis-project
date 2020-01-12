"""
Driver to fine-tune detection using time + openpose 2D keypoints.

For preprocessing, run run_openpose.py which will compute bbox trajectories.

Assumes there's only one person in the video FTM.
Also assumes that the person is visible for a contiguous duration.

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function


import cv2, os, sys, json
from absl import flags
import numpy as np
from os.path import exists, join, basename, dirname
from os import makedirs
import tempfile
import shutil
from os import system
from glob import glob
import deepdish as dd
from imageio import imwrite

import tensorflow as tf

def del_all_flags(FLAGS):
    FLAGS.remove_flag_values(FLAGS.flag_values_dict())

del_all_flags(tf.flags.FLAGS)

from hmr.smpl_webuser.serialization import load_model
parentdir = os.path.dirname('hmr/')
sys.path.insert(0,parentdir) 
from src.config import get_config
from src.util.video import read_data, collect_frames
from src.util.renderer import SMPLRenderer, draw_openpose_skeleton, render_original
from src.refiner import Refiner
# from jason.bvh_core import write2bvh


# Defaults:
kVidDir = 'data/vid'
# Where the smoothed results will be stored.
kOutDir = 'refined'
# Holds h5 for each video, which stores OP outputs, after trajectory assignment.
kOpDir = 'out'


kMaxLength = 1000
kVisThr = 0.2
RENDONLY = False

# set if you only want to render specific renders.
flags.DEFINE_string('render_only', '', 'If not empty and are either {mesh, mesh_only}, only renders that result.')
flags.DEFINE_string('vid_dir', kVidDir, 'directory with videso')
flags.DEFINE_string('out_dir', kOutDir, 'directory to output results')
flags.DEFINE_string('op_dir', kOpDir,
                    'directory where openpose output is')


model = None
sess = None


def run_video(frames, per_frame_people, config, out_mov_path):
    """
    1. Extract all frames, preprocess it
    2. Send it to refiner, get 3D pose back.

    Render results.
    """
    proc_imgs, proc_kps, proc_params, start_fr, end_fr = collect_frames(
        frames, per_frame_people, config.img_size, vis_thresh=kVisThr)

    num_frames = len(proc_imgs)

    proc_imgs = np.vstack(proc_imgs)

    out_res_path = out_mov_path.replace('.mp4', '.h5')

    if not exists(out_res_path) or config.viz:
        # Run HMR + refinement.
        tf.reset_default_graph()
        model = Refiner(config, num_frames)
        scale_factors = [np.mean(pp['scale'])for pp in proc_params]
        offsets = np.vstack([pp['start_pt'] for pp in proc_params])
        results = model.predict(proc_imgs, proc_kps, scale_factors, offsets)
        # Pack proc_param into result.
        results['proc_params'] = proc_params

        # Pack results:
        result_dict = {}
        used_frames = frames[start_fr:end_fr + 1]
        for i, (frame, proc_param) in enumerate(zip(used_frames, proc_params)):
            bbox = proc_param['bbox']
            op_kp = proc_param['op_kp']

            # Recover verts from SMPL params.
            theta = results['theta'][i]
            pose = theta[3:3+72]
            shape = theta[3+72:]
            smpl.trans[:] = 0.
            smpl.betas[:] = shape
            smpl.pose[:] = pose
            verts = smpl.r

            result_here = {
                'theta': np.expand_dims(theta, 0),
                'joints': np.expand_dims(results['joints'][i], 0),
                'cams': results['cams'][i],
                'joints3d': results['joints3d'][i],
                'verts': verts,
                'op_kp': op_kp,
                'proc_param': proc_param
            }
            result_dict[i] = [result_here]
        # Save result in JSON format
        listified_result = {}
        for key in result_dict:
            listified_result[key] = {}
            for key2 in result_dict[key][0]:
                if key2 == 'proc_param':
                    continue
                listified_result[key][key2] = result_dict[key][0][key2].tolist()
        json.dump(listified_result,open(out_res_path.replace('.h5', '.json'),'w'))
        # Save results & write bvh.
        dd.io.save(out_res_path, result_dict)
        # TODO.
        # bvh_path = out_res_path.replace('.h5', '.bvh')
        # if not exists(bvh_path):
        #     write2bvh(out_res_path, bvh_path)
    else:
        result_dict = dd.io.load(out_res_path)

    # Render results into video.
    temp_dir = 'temp/' + str(np.random.randint(10000))
    if not exists(temp_dir):
        makedirs(temp_dir)
    print('writing to %s' % temp_dir)

    used_frames = frames[start_fr:end_fr + 1]

    #theta_list = hmr(used_frame)

    for i, (frame, proc_param) in enumerate(zip(used_frames, proc_params)):
        if i % 10 == 0:
            print('%d/%d' % (i, len(used_frames)))

        result_here = result_dict[i][0]

        # Render each frame.
        if RENDONLY and 'only' in REND_TYPE:
            rend_frame = np.ones_like(frame)
            skel_frame = np.ones_like(frame) * 255
            op_frame = np.ones_like(frame) * 255
        else:
            rend_frame = frame.copy()
            skel_frame = frame.copy()
            op_frame = frame.copy()

        op_frame = cv2.putText(
            op_frame.copy(),
            'OpenPose Output', (10, 50),
            0,
            1,
            tuple([int(0) for _ in range(3)]) ,
            thickness=3)
        other_vp = np.ones_like(frame)
        other_vp2 = np.ones_like(frame)

        op_kp = result_here['op_kp']
        bbox = result_here['proc_param']['bbox']
        op_frame = draw_openpose_skeleton(op_frame, op_kp)

        if not RENDONLY or (RENDONLY and 'op' not in REND_TYPE):
            rend_frame, skel_frame, other_vp, other_vp2 = render_original(
                rend_frame,
                skel_frame,
                proc_param,
                result_here,
                other_vp,
                other_vp2,
                bbox, 
                renderer)
            #add theta_list_rendered
            row1 = np.hstack((frame, skel_frame, np.ones_like(op_frame) * 255))
            row2 = np.hstack((rend_frame, other_vp2[:, :, :3], op_frame))
            final_rend_img = np.vstack((row2, row1)).astype(np.uint8)

        if RENDONLY:
            if 'mesh' in REND_TYPE:
                final_rend_img = rend_frame.astype(np.uint8)
            elif 'op' in REND_TYPE:
                final_rend_img = op_frame.astype(np.uint8)
            else:
                final_rend_img = skel_frame.astype(np.uint8)

        import matplotlib.pyplot as plt
        # plt.ion()
        # plt.figure(1)
        # plt.clf()
        # #plt.imshow(final_rend_img)
        # plt.title('%d/%d' % (i, len(used_frames)))
        # plt.pause(1e-3)

        out_name = join(temp_dir, 'frame%03d.png' % i)
        imwrite(out_name, final_rend_img)
    output_video_path = out_mov_path.replace(".h5", ".mp4")
    # Write video.
    cmd = 'ffmpeg -y -threads 16  -i %s/frame%%03d.png -profile:v baseline -level 3.0 -c:v libx264 -pix_fmt yuv420p -an -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" %s' % (
        temp_dir, output_video_path)
    system(cmd)
    #shutil.rmtree(temp_dir)


def get_pred_prefix(load_path):
    """
    Figure out the save name.
    """
    checkpt_name = basename(load_path)
    model_name = basename(dirname(config.load_path))

    prefix = []

    if config.refine_inpose:
        prefix += ['OptPose']

    prefix += ['kpw%.2f' % config.e_loss_weight]
    prefix += ['shapew%.2f' % config.shape_loss_weight]
    prefix += ['jointw%.2f' % config.joint_smooth_weight]
    if config.use_weighted_init_pose:
        prefix += ['init-posew%.2f-weighted' % config.init_pose_loss_weight]
    else:
        prefix += ['init-posew%.2f' % config.init_pose_loss_weight]
    if config.camera_smooth_weight > 0:
        prefix += ['camw%.2f' % config.camera_smooth_weight]

    prefix += ['numitr%d' % config.num_refine]

    prefix = '_'.join(prefix)
    if 'Feb12_2100' not in model_name:
        pred_dir = join(config.out_dir, model_name + '-' + checkpt_name, prefix)
    else:
        if prefix == '':
            save_prefix = checkpt_name
        else:
            save_prefix = prefix + '_' + checkpt_name

        pred_dir = join(config.out_dir, save_prefix)

    if RENDONLY:
        pred_dir = join(pred_dir, REND_TYPE)

    print('\n***\nsaving output in %s\n***\n' % pred_dir)

    if not exists(pred_dir):
        makedirs(pred_dir)

    return pred_dir


def main(config):
    np.random.seed(5)
    video_paths = sorted(glob(join(config.vid_dir, "*.mp4")))
    # Figure out the save name.
    pred_dir = 'refined'
    # import ipdb; ipdb.set_trace()

    for i, vid_path in enumerate(video_paths[:]):
        out_mov_path = join(pred_dir, basename(vid_path).replace('.mp4', '.h5'))
        if not exists(out_mov_path) or config.viz:
            print('working on %s' % basename(vid_path))
            frames, per_frame_people, valid = read_data(vid_path, config.op_dir, max_length=kMaxLength)
            if valid:
                run_video(frames, per_frame_people, config, out_mov_path)

    print('Finished writing to %s' % pred_dir)


if __name__ == '__main__':
    config = get_config()
    
    config.load_path = os.getcwd()+"/hmr/models/model.ckpt-667589"
    # config.load_path = os.getcwd()+"/hmr/models/hmr_rotaug_May03_1425/model.ckpt-1111959"

    if len(config.render_only) > 0:
        RENDONLY = True
        REND_TYPE = config.render_only
        rend_types = ['mesh', 'mesh_only', 'op', 'op_only']
        if not np.any(np.array([REND_TYPE == rend_t for rend_t in rend_types])):
            print('Unknown rend type %s!' % REND_TYPE)
            import ipdb; ipdb.set_trace()

    if not config.load_path:
        raise Exception('Must specify a model to use to predict!')
    if 'model.ckpt' not in config.load_path:
        raise Exception('Must specify a model checkpoint!')

    if not exists(config.out_dir):
        makedirs(config.out_dir)

    # For visualization.
    renderer = SMPLRenderer(img_size=config.img_size, flength=1000.,
                            face_path=config.smpl_face_path)
    smpl = load_model(config.smpl_model_path)

    main(config)