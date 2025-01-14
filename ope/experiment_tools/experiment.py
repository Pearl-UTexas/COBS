import json
import argparse
import matplotlib as mpl
import matplotlib.pyplot as plt
import scipy.signal as signal
import os
from skimage.transform import rescale, resize, downscale_local_mean
import json
from collections import OrderedDict, Counter
import tensorflow as tf
from keras.models import load_model, model_from_json
from keras import backend as K
import time
import argparse
import boto3
import glob
import numpy as np
import sys
from pdb import set_trace as b
import numpy as np
from skimage.color import rgb2gray
from skimage.transform import resize

from ope.algos.doubly_robust_v2 import DoublyRobust_v2 as DR
from ope.algos.fqe import FittedQEvaluation
from ope.algos.magic import MAGIC
from ope.algos.average_model import AverageModel as AM
from ope.algos.sequential_DR import SeqDoublyRobust as SeqDR
from ope.algos.dm_regression import DirectMethodRegression as DM
from ope.algos.traditional_is import TraditionalIS as IS
from ope.algos.infinite_horizon import InfiniteHorizonOPE as IH
from ope.algos.event_is import EventIS 
from ope.algos.dm_regression import DirectMethodRegression
from ope.algos.more_robust_doubly_robust import MRDR
from ope.algos.retrace_lambda import Retrace

from ope.models.approximate_model import ApproxModel
from ope.models.basics import BasicPolicy
from ope.models.epsilon_greedy_policy import EGreedyPolicy
from ope.models.max_likelihood import MaxLikelihoodModel
from ope.models.Q_wrapper import QWrapper
from ope.models.tabular_model import TabularPolicy

from ope.utls.get_Qs import getQs
from ope.utls.rollout import rollout, Data

from ope.utils import keyboard

def analysis(dic):

    divergence = -1
    if 'KLDivergence' in dic:
        divergence = dic['KLDivergence']
        del dic['KLDivergence']

    longest = max([len(key) for key,_ in dic.items()])
    sorted_keys = np.array([[key,val[1]] for key,val in dic.items()])
    sorted_keys = sorted_keys[np.argsort(sorted_keys[:,1].astype(float))]

    # sorted_keys = sorted_keys[sorted(sorted_ke)]
    print ("Results:  \n")
    for key, value in dic.items():
        label = ' '*(longest-len(key)) + key
        print("{}: {:10.4f}. Error: {:10.4f}".format(label, *value))
    print('\n')
    print ("Ordered Results:  \n")
    for key in sorted_keys[:,0]:
        value = dic[key]
        label = ' '*(longest-len(key)) + key
        print("{}: {:10.4f}. Error: {:10.4f}".format(label, *value))

    dic['KLDivergence'] = divergence
    return dic

class Result(object):
    def __init__(self, cfg, result):
        self.cfg = cfg
        self.result = result

class ExperimentRunner(object):
    def __init__(self):
        self.results = []
        self.cfgs = []

    def add(self, cfg):
        self.cfgs.append(cfg)

    def run(self):
        """
        Returns:
          result: a dictionary mapping name of estimator to a list s.t. result[NAME][0] is 
            the estimate and result[NAME][1] is the error
        """
        results = []

        trial = 1
        all_eval_data = None
        for cfg in self.cfgs:
            trial += 1

            if cfg.modeltype == 'tabular':
                all_Nval_results, trial_eval_data = self.run_tabular(cfg, all_eval_data, trial)
                results += all_Nval_results

                if all_eval_data is None:
                    all_eval_data = trial_eval_data

            else:
                all_Nval_results, trial_eval_data = self.run_NN(cfg, all_eval_data, trial)
                results += all_Nval_results

                if all_eval_data is None:
                    all_eval_data = trial_eval_data

        return results

    def get_rollout(self, cfg, eval_data=False, N_overwrite = None):
        env = cfg.env
        pi_e = cfg.pi_e
        pi_b = cfg.pi_b
        processor = cfg.processor
        absorbing_state = cfg.absorbing_state
        T = cfg.horizon
        frameskip = cfg.frameskip if cfg.frameskip is not None else 1
        frameheight = cfg.frameheight if cfg.frameheight is not None else 1
        use_only_last_reward = cfg.use_only_last_reward if cfg.use_only_last_reward is not None else False

        if eval_data:
            data = rollout(env, pi_e, processor, absorbing_state, N=max(10000, cfg.num_traj) if N_overwrite is None else N_overwrite, T=T, frameskip=frameskip, frameheight=frameheight, path=None, filename='tmp', use_only_last_reward=use_only_last_reward)
        else:
            data = rollout(env, pi_b, processor, absorbing_state, pi_e = pi_e, N=cfg.num_traj, T=T, frameskip=frameskip, frameheight=frameheight, path=None, filename='tmp',use_only_last_reward=use_only_last_reward)

        return data


    def run_tabular(self, cfg, all_eval_data=None, trial=None):
        env = cfg.env
        pi_e = cfg.pi_e
        pi_b = cfg.pi_b
        processor = cfg.processor
        absorbing_state = cfg.absorbing_state
        T = cfg.horizon
        gamma = cfg.gamma
        models = cfg.models

        if isinstance(models, str):
            if models == 'all':
                models = ['MFree_Retrace_L', 'MFree_MRDR', 'MFree_IH', 'MFree_FQE', 'MBased_MLE', 'MFree_Reg', 'IS', 'MFree_NStep']
            elif models == 'n-step':
                models = ['MFree_NStep', 'MFree_IH', 'IS']
            else:
              raise ValueError("Please give valid value of models")

        # Generate dataset for the largest data size.
        if all_eval_data is None:
            all_eval_data = rollout(env, pi_e, processor, absorbing_state, N=max(10000, cfg.num_traj), T=T, frameskip=1, frameheight=1, path=None, filename='tmp',)
        all_behavior_data = rollout(env, pi_b, processor, absorbing_state, pi_e = pi_e, N=cfg.num_traj, T=T, frameskip=1, frameheight=1, path=None, filename='tmp',)

        if cfg.to_regress_pi_b:
            all_behavior_data.estimate_propensity()

        true = all_eval_data.value_of_data(gamma, False)
        print('V(pi_b): ',all_behavior_data.value_of_data(gamma, False), 'V(pi_b) Normalized: ',all_behavior_data.value_of_data(gamma, True))
        print('V(pi_e): ',all_eval_data.value_of_data(gamma, False), 'V(pi_e) Normalized: ',all_eval_data.value_of_data(gamma, True))


        # Compute the estimate for different amounts of data.
        all_Nval_results = []
        for Nval in cfg.Nvals: 

            # Store results for computation for this Nval.
            dic = {}
            dic.update({'ON POLICY': [float(true), 0]})

            # Get first Nval trajectories in dataset.
            subset_behavior_data = Data(all_behavior_data.trajectories[:Nval],        
                                        all_behavior_data.n_actions,
                                        all_behavior_data.n_dim,
                                        all_behavior_data.make_int)

            # Compute Qs.
            get_Qs = getQs(subset_behavior_data, pi_e, processor, env.n_actions)


            # Compute each type of estimate.
            for model in models:
                if model == 'MBased_MLE':
                    env_model = MaxLikelihoodModel(gamma, max_traj_length=T, action_space_dim=env.n_actions)
                    env_model.run(subset_behavior_data)
                    Qs_model_based = get_Qs.get(env_model)

                    out = self.estimate(Qs_model_based, subset_behavior_data, gamma, 'Model Based', true)
                    dic.update(out)
                elif model == 'MBased_Approx':
                    print('*'*20)
                    print('Approx estimator not implemented for tabular state space. Please use MBased_MLE instead')
                    print('*'*20)
                elif model == 'MFree_Reg':
                    DMRegression = DirectMethodRegression(subset_behavior_data, gamma, None, None, None)
                    dm_model_ = DMRegression.run(pi_b, pi_e)
                    dm_model = QWrapper(dm_model_, {}, is_model=True, modeltype='linear', action_space_dim=env.n_actions)
                    Qs_DM_based = get_Qs.get(dm_model)

                    out = self.estimate(Qs_DM_based, subset_behavior_data, gamma,'DM Regression', true)
                    dic.update(out)
                elif model == 'MFree_FQE':
                    FQE = FittedQEvaluation(subset_behavior_data, gamma)
                    out0, Q, mapping = FQE.run(pi_b, pi_e)
                    fqe_model = QWrapper(Q, mapping, is_model=False, action_space_dim=env.n_actions)
                    Qs_FQE_based = get_Qs.get(fqe_model)

                    out = self.estimate(Qs_FQE_based, subset_behavior_data, gamma, 'FQE', true)
                    dic.update(out)
                elif model == 'MFree_IH':
                    ih_max_epochs = None
                    matrix_size = None
                    inf_horizon = IH(subset_behavior_data, 30, 1e-3, 3e-3, gamma, True, None, env=env, weighted=cfg.weighted)
                    inf_hor_output = inf_horizon.evaluate(env, ih_max_epochs, matrix_size)
                    inf_hor_output /= 1/np.sum(gamma ** np.arange(max(subset_behavior_data.lengths())))
                    dic.update({'IH': [inf_hor_output, (inf_hor_output - true )**2]})
                elif model == 'MFree_NStep': 
                    event_max_epochs = None
                    matrix_size = None
                    event_is = EventIS(data=subset_behavior_data, w_hidden=30, Learning_rate=1e-4, reg_weight=3e-3, gamma=gamma, discrete=True, modeltype=None, env=env, interp_type="n-step", weighted=cfg.weighted)
                    if (cfg.nstep_custom_ns is not None):
                        all_event_output = event_is.evaluate(env, event_max_epochs, matrix_size, nstep_custom_ns=cfg.nstep_custom_ns)
                        for i in cfg.nstep_custom_ns:
                            dic.update({'NStep(t=%d)'%i: [all_event_output[i], (all_event_output[i] - true )**2]})
                    else:
                        all_event_output = event_is.evaluate(env, event_max_epochs, matrix_size, nstep_int=cfg.nstep_int)
                        for i in range(0, subset_behavior_data.states().shape[1]+1, cfg.nstep_int):
                            dic.update({'NStep(t=%d)'%i: [all_event_output[i], (all_event_output[i] - true )**2]})

                elif model == 'MFree_MRDR':
                    mrdr = MRDR(subset_behavior_data, gamma, modeltype = 'tabular')
                    _ = mrdr.run(pi_e)
                    mrdr_model = QWrapper(mrdr, {}, is_model=True, modeltype='linear', action_space_dim=env.n_actions) # annoying missname of variable. fix to be modeltype='tabular'
                    Qs_mrdr_based = get_Qs.get(mrdr_model)

                    out = self.estimate(Qs_mrdr_based, subset_behavior_data, gamma, 'MRDR', true)
                    dic.update(out)
                elif model == 'MFree_Retrace_L':
                    retrace = Retrace(subset_behavior_data, gamma, lamb=1.)
                    out0, Q, mapping = retrace.run(pi_b, pi_e, 'retrace', epsilon=.001)
                    retrace_model = QWrapper(Q, mapping, is_model=False, action_space_dim=env.n_actions)
                    Qs_retrace_based = get_Qs.get(retrace_model)

                    out = self.estimate(Qs_retrace_based, subset_behavior_data, gamma, 'Retrace(lambda)', true)
                    dic.update(out)

                    out0, Q, mapping = retrace.run(pi_b, pi_e, 'tree-backup', epsilon=.001)
                    retrace_model = QWrapper(Q, mapping, is_model=False, action_space_dim=env.n_actions)
                    Qs_retrace_based = get_Qs.get(retrace_model)

                    out = self.estimate(Qs_retrace_based, subset_behavior_data, gamma, 'Tree-Backup', true)
                    dic.update(out)

                    out0, Q, mapping = retrace.run(pi_b, pi_e, 'Q^pi(lambda)', epsilon=.001)
                    retrace_model = QWrapper(Q, mapping, is_model=False, action_space_dim=env.n_actions)
                    Qs_retrace_based = get_Qs.get(retrace_model)

                    out = self.estimate(Qs_retrace_based, subset_behavior_data, gamma, 'Q^pi(lambda)', true)
                    dic.update(out)

                elif model == 'IS':
                    out = self.estimate([], subset_behavior_data, gamma, 'IS', true, True)
                    dic.update(out)
                else:
                    print(model, ' is not a valid method')

            result = analysis(dic)
            result["Nval"]  = Nval
            self.results.append(Result(cfg, result))
            all_Nval_results.append(result)

        return all_Nval_results, all_eval_data


    def run_NN(self, cfg, all_eval_data=None, trial=None):
        env = cfg.env
        pi_e = cfg.pi_e
        pi_b = cfg.pi_b
        processor = cfg.processor
        absorbing_state = cfg.absorbing_state
        T = cfg.horizon
        gamma = cfg.gamma
        models = cfg.models
        frameskip = cfg.frameskip
        frameheight = cfg.frameheight
        modeltype = cfg.modeltype
        Qmodel = cfg.Qmodel


        if isinstance(models, str):
            if models == 'all':
                models = ['MFree_Retrace_L', 'MFree_MRDR', 'MFree_IH', 'MFree_FQE', 'MBased_MLE', 'MFree_Reg', 'IS', 'MFree_NStep']
            elif models == 'n-step':
                models = ['MFree_NStep', 'IS']
            else:
              raise ValueError("Please give valid value of models")

        if all_eval_data is None:
            all_eval_data = rollout(env, pi_e, processor, absorbing_state, N=max(10000, cfg.num_traj), T=T, frameskip=frameskip, frameheight=frameheight, path=None, filename='tmp',)
        all_behavior_data = rollout(env, pi_b, processor, absorbing_state, pi_e = pi_e, N=cfg.num_traj, T=T, frameskip=frameskip, frameheight=frameheight, path=None, filename='tmp',)

        if cfg.convert_from_int_to_img is not None:
            traj = []
            for trajectory in behavior_data.trajectories:
                frames = []
                for frame in trajectory['frames']:
                    frames.append(cfg.convert_from_int_to_img(np.array(frame)))
                traj.append(frames)
            for i,frames in enumerate(traj):
                behavior_data.trajectories[i]['frames'] = frames

        if cfg.to_regress_pi_b:
            behavior_data.estimate_propensity()

        true = all_eval_data.value_of_data(gamma, False)
        print('V(pi_b): ', all_behavior_data.value_of_data(gamma, False), 'V(pi_b) Normalized: ',all_behavior_data.value_of_data(gamma, True))
        print('V(pi_e): ', all_eval_data.value_of_data(gamma, False), 'V(pi_e) Normalized: ', all_eval_data.value_of_data(gamma, True))


        # Compute the estimate for different amounts of data.
        all_Nval_results = []
        for Nval in cfg.Nvals:

            # Store results for computation for this Nval.
            dic = {}
            dic.update({'ON POLICY': [float(true), 0]})

            # Get first Nval trajectories in dataset.
            subset_behavior_data = Data(all_behavior_data.trajectories[:Nval],        
                                        all_behavior_data.n_actions,
                                        all_behavior_data.n_dim,
                                        all_behavior_data.make_int)


            get_Qs = getQs(subset_behavior_data, pi_e, processor, env.n_actions)

            # Compute each type of estimate.
            for model in models:
                if (model == 'MBased_Approx') or (model == 'MBased_MLE'):
                    if model == 'MBased_MLE':
                        print('*'*20)
                        print('MLE estimator not implemented for continuous state space. Using MBased_Approx instead')
                        print('*'*20)
                    MBased_max_trajectory_length = 25
                    batchsize = 32
                    mbased_num_epochs = 100
                    MDPModel = ApproxModel(gamma, None, MBased_max_trajectory_length, frameskip, frameheight, processor, action_space_dim=env.n_actions)
                    mdpmodel = MDPModel.run(env, subset_behavior_data, mbased_num_epochs, batchsize, Qmodel)

                    Qs_model_based = get_Qs.get(mdpmodel)
                    out = self.estimate(Qs_model_based, subset_behavior_data, gamma,'MBased_Approx', true)
                    dic.update(out)

                elif model == 'MFree_Reg':
                    DMRegression = DirectMethodRegression(subset_behavior_data, gamma, frameskip, frameheight, Qmodel, processor)
                    dm_max_epochs = 80
                    _,dm_model_Q = DMRegression.run_NN(env, pi_b, pi_e, dm_max_epochs, epsilon=0.001)

                    dm_model = QWrapper(dm_model_Q, None, is_model=True, action_space_dim=env.n_actions, modeltype=modeltype)
                    Qs_DM_based = get_Qs.get(dm_model)

                    out = self.estimate(Qs_DM_based, subset_behavior_data, gamma,'DM Regression', true)
                    dic.update(out)
                elif model == 'MFree_FQE':
                    FQE = FittedQEvaluation(subset_behavior_data, gamma, frameskip, frameheight, Qmodel, processor)

                    fqe_max_epochs = 80
                    _,_,fqe_Q = FQE.run_NN(env, pi_b, pi_e, fqe_max_epochs, epsilon=0.0001)

                    fqe_model = QWrapper(fqe_Q, None, is_model=True, action_space_dim=env.n_actions, modeltype=modeltype)
                    Qs_FQE_based = get_Qs.get(fqe_model)

                    out = self.estimate(Qs_FQE_based, subset_behavior_data, gamma, 'FQE', true)
                    dic.update(out)
                elif model == 'MFree_IH':
                    # ih_max_epochs = 10001
                    ih_matrix_size = 1024
                    inf_horizon = IH(subset_behavior_data, 30, 1e-3, 3e-3, gamma, False, Qmodel, processor=processor)
                    inf_hor_output = inf_horizon.evaluate(env, cfg.max_epochs, ih_matrix_size)
                    inf_hor_output /= 1/np.sum(gamma ** np.arange(max(subset_behavior_data.lengths())))
                    dic.update({'IH': [inf_hor_output, (inf_hor_output - true )**2]})
                elif model == 'MFree_NStep':
                    event_matrix_size = 1024
                    event_is = EventIS(subset_behavior_data, 30, 1e-3, 3e-3, gamma, False, Qmodel, interp_type="n-step", env=env, processor=processor)
                    if (cfg.nstep_custom_ns is not None):
                        all_event_output = event_is.evaluate(env, cfg.max_epochs, event_matrix_size, nstep_custom_ns=cfg.nstep_custom_ns)
                        for i in cfg.nstep_custom_ns:
                            dic.update({'NStep(t=%d)'%i: [all_event_output[i], (all_event_output[i] - true )**2]})
                    else:
                        all_event_output = event_is.evaluate(env, cfg.max_epochs, event_matrix_size, nstep_int=cfg.nstep_int)
                        for i in range(0, subset_behavior_data.states().shape[1]+1, cfg.nstep_int):
                            dic.update({'NStep(t=%d)'%i: [all_event_output[i], (all_event_output[i] - true )**2]})
                elif model == 'MFree_MRDR':
                    mrdr = MRDR(subset_behavior_data, gamma, frameskip, frameheight, Qmodel, processor)

                    mrdr_max_epochs = 80
                    mrdr_matrix_size = 1024
                    _,_,mrdr_Q = mrdr.run_NN(env, pi_b, pi_e, mrdr_max_epochs, mrdr_matrix_size, epsilon=0.001)
                    mrdr_model = QWrapper(mrdr_Q, None, is_model=True, action_space_dim=env.n_actions, modeltype=modeltype)
                    Qs_mrdr_based = get_Qs.get(mrdr_model)

                    out = self.estimate(Qs_mrdr_based, subset_behavior_data, gamma, 'MRDR', true)
                    dic.update(out)
                elif model == 'MFree_Retrace_L':
                    retrace = Retrace(subset_behavior_data, gamma, frameskip, frameheight, Qmodel, lamb=.9, processor=processor)

                    retrace_max_epochs = 80
                    _,_,retrace_Q = retrace.run_NN(env, pi_b, pi_e, retrace_max_epochs, 'retrace', epsilon=0.001)
                    retrace_model = QWrapper(retrace_Q, None, is_model=True, action_space_dim=env.n_actions, modeltype=modeltype) # use mlp-based wrapper even for linear
                    Qs_retrace_based = get_Qs.get(retrace_model)
                    out = self.estimate(Qs_retrace_based, subset_behavior_data, gamma, 'Retrace(lambda)', true)
                    dic.update(out)

                    _,_,tree_Q = retrace.run_NN(env, pi_b, pi_e, retrace_max_epochs, 'tree-backup', epsilon=0.001)
                    tree_model = QWrapper(tree_Q, None, is_model=True, action_space_dim=env.n_actions, modeltype=modeltype)
                    Qs_tree_based = get_Qs.get(tree_model)
                    out = self.estimate(Qs_tree_based, subset_behavior_data, gamma, 'Tree-Backup', true)
                    dic.update(out)

                    _,_,q_lambda_Q = retrace.run_NN(env, pi_b, pi_e, retrace_max_epochs, 'Q^pi(lambda)', epsilon=0.001)
                    q_lambda_model = QWrapper(q_lambda_Q, None, is_model=True, action_space_dim=env.n_actions, modeltype=modeltype)
                    Qs_q_lambda_based = get_Qs.get(q_lambda_model)
                    out = self.estimate(Qs_q_lambda_based, subset_behavior_data, gamma, 'Q^pi(lambda)', true)
                    dic.update(out)

                elif model == 'IS':
                    out = self.estimate([], subset_behavior_data, gamma, 'IS', true, True)
                    dic.update(out)
                else:
                    print(model, ' is not a valid method')

                analysis(dic)

            result = analysis(dic)
            result["Nval"] = Nval
            self.results.append(Result(cfg, result))
            all_Nval_results.append(result)

        return all_Nval_results, all_eval_data

    def estimate(self, Qs, data, gamma, name, true, IS_eval=False):
        dic = {}
        dr = DR(gamma)
        mag = MAGIC(gamma)
        am = AM(gamma)
        sdr = SeqDR(gamma)
        imp_samp = IS(gamma)
        num_j_steps = 25

        info = [data.actions(),
                data.rewards(),
                data.base_propensity(),
                data.target_propensity(),
                Qs
                ]

        if IS_eval:
            IS_eval = imp_samp.evaluate(info)
            dic['NAIVE']     = [float(IS_eval[0]), float( (IS_eval[0] - true )**2)]
            dic['IS']        = [float(IS_eval[1]), float( (IS_eval[1] - true )**2)]
            dic['STEP IS']   = [float(IS_eval[2]), float( (IS_eval[2] - true )**2)]
            dic['WIS']       = [float(IS_eval[3]), float( (IS_eval[3] - true )**2)]
            dic['STEP WIS']  = [float(IS_eval[4]), float( (IS_eval[4] - true )**2)]
        else:
            dr_evaluation = dr.evaluate(info)
            wdr_evaluation = dr.evaluate(info, True)
            magic_evaluation = mag.evaluate(info, num_j_steps, True)
            AM_evaluation = am.evaluate(info)
            SDR_evaluation = sdr.evaluate(info)
            dic['AM {0}'.format(name)] = [AM_evaluation, (AM_evaluation - true)**2]
            dic['DR {0}'.format(name)] = [dr_evaluation, (dr_evaluation - true)**2]
            dic['WDR {0}'.format(name)] = [wdr_evaluation, (wdr_evaluation - true)**2]
            dic['MAGIC {0}'.format(name)] = [magic_evaluation[0], (magic_evaluation[0] - true )**2]
            dic['SDR {0}'.format(name)] = [SDR_evaluation[0], (SDR_evaluation[0] - true )**2]

        # return dr_evaluation, wdr_evaluation, magic_evaluation, AM_evaluation, SDR_evaluation
        return dic
