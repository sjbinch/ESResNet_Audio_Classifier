# -*- coding: utf-8 -*-
# @Time    : 6/19/21 12:23 AM
# @Author  : Yuan Gong
# @Affiliation  : Massachusetts Institute of Technology
# @Email   : yuangong@mit.edu
# @File    : dataloader.py

# modified from:
# Author: David Harwath
# with some functions borrowed from https://github.com/SeanNaren/deepspeech.pytorch

import csv
import json
import torchaudio
import librosa
import numpy as np
from numpy.fft import fft
from mosqito.utils.time_segmentation import time_segmentation
import torch
import torch.nn.functional
from torch.utils.data import Dataset
import random

def make_index_dict_mdps(num_class):
    if num_class == 2:
        return {'Normal':0, 'GRR':1, 'DOL':1, 'SG':1}
    elif num_class == 3:
        return {'Normal':0, 'GRR':1, 'DOL':1, 'SG':2}
    elif num_class == 4:
        return {'Normal':0, 'GRR':1, 'DOL':2, 'SG':3}

def make_index_dict(label_csv):
    index_lookup = {}
    with open(label_csv, 'r') as f:
        csv_reader = csv.DictReader(f)
        line_count = 0
        for row in csv_reader:
            index_lookup[row['mid']] = row['index']
            line_count += 1
    return index_lookup

def make_name_dict(label_csv):
    name_lookup = {}
    with open(label_csv, 'r') as f:
        csv_reader = csv.DictReader(f)
        line_count = 0
        for row in csv_reader:
            name_lookup[row['index']] = row['display_name']
            line_count += 1
    return name_lookup

def lookup_list(index_list, label_csv):
    label_list = []
    table = make_name_dict(label_csv)
    for item in index_list:
        label_list.append(table[item])
    return label_list

def preemphasis(signal,coeff=0.97):
    """perform preemphasis on the input signal.

    :param signal: The signal to filter.
    :param coeff: The preemphasis coefficient. 0 is none, default 0.97.
    :returns: the filtered signal.
    """
    return np.append(signal[0],signal[1:]-coeff*signal[:-1])

class AudiosetDataset(Dataset):
    def __init__(self, dataset_json_file, audio_conf, label_csv=None, mdps=False, sample_type=''):
        """
        Dataset that manages audio recordings
        :param audio_conf: Dictionary containing the audio loading and preprocessing settings
        :param dataset_json_file
        """
        self.datapath = dataset_json_file
        with open(dataset_json_file, 'r') as fp:
            data_json = json.load(fp)

        self.data = data_json['data']
        self.audio_conf = audio_conf
        print('---------------the {:s} dataloader---------------'.format(self.audio_conf.get('mode')))
        self.melbins = self.audio_conf.get('num_mel_bins') # num_mel_bins = 128
        self.freqm = self.audio_conf.get('freqm') # freqm = 24
        self.timem = self.audio_conf.get('timem') # timem = 96
        print('now using following mask: {:d} freq, {:d} time'.format(self.audio_conf.get('freqm'), self.audio_conf.get('timem')))
        self.mixup = self.audio_conf.get('mixup') # mixup = 0
        print('now using mix-up with rate {:f}'.format(self.mixup))
        self.dataset = self.audio_conf.get('dataset') # dataset = mdps
        print('now process ' + self.dataset)
        # dataset spectrogram mean and std, used to normalize the input
        self.norm_mean = self.audio_conf.get('mean')
        self.norm_std = self.audio_conf.get('std')
        # skip_norm is a flag that if you want to skip normalization to compute the normalization stats using src/get_norm_stats.py, if Ture, input normalization will be skipped for correctly calculating the stats.
        # set it as True ONLY when you are getting the normalization stats.
        self.skip_norm = self.audio_conf.get('skip_norm') if self.audio_conf.get('skip_norm') else False # False
        if self.skip_norm:
            print('now skip normalization (use it ONLY when you are computing the normalization stats).')
        else:
            print('use dataset mean {:.3f} and std {:.3f} to normalize the input.'.format(self.norm_mean, self.norm_std))
        # if add noise for data augmentation
        self.noise = self.audio_conf.get('noise') # False
        if self.noise == True:
            print('now use noise augmentation')

        self.mdps = mdps
        self.sample_type = sample_type
        self.index_dict = make_index_dict_mdps(num_class=audio_conf.get('num_class')) if mdps else make_index_dict(label_csv)
        self.label_num = len(set(self.index_dict.values()))
        print('number of classes is {:d}'.format(self.label_num))


    def _wav2stft(self, filename):
        input_array, sr = librosa.load(filename, sr=12800, mono=True)
        nperseg = 2048 # fs = 12800Hz 기준 Δt = 0.16s(Δf = 6.25Hz)
        noverlap = 384*4 # hop size(noverlap = TL time increment)

        sig, _ = time_segmentation(
                input_array, sr, nperseg=nperseg, noverlap=noverlap, is_ecma=False
            )

        sig = torch.from_numpy(sig).half()

        nfft = sig.shape[0]
        nseg = sig.shape[1]
        window = np.hanning(nfft)
        window = np.tile(window,(nseg,1)).T

        spec = fft(sig, n=nfft, axis=0)[0:nfft//2]
        spec_abs = 2*abs(spec)/nfft

        if 'LINE' in self.sample_type:
            spec_abs = 20 * (np.log10(spec_abs*1e6)) # 진동데이터는 50000 대신 10**6
        else:
            spec_abs = 20 * (np.log10(spec_abs*50000)) # 진동데이터는 50000 대신 10**6

        if 'LINE' in self.sample_type:
            result = torch.from_numpy(spec_abs).half().T
        else:
            stft_result = torch.from_numpy(spec_abs).half()
            freqs = np.linspace(0, 12800 / 2, stft_result.size(0))
            a_weighting_db = librosa.A_weighting(freqs)
            a_weighting_scale_tensor = torch.from_numpy(a_weighting_db).half()
            a_weighted_stft = stft_result + a_weighting_scale_tensor.unsqueeze(1)
            a_weighted_stft = a_weighted_stft.T
            result = torch.where(a_weighted_stft < 0.0, torch.tensor(0, dtype=a_weighted_stft.dtype), a_weighted_stft)

        target_length = self.audio_conf.get('target_length')
        n_frames = result.shape[0]

        p = target_length - n_frames

        # cut and pad
        if p > 0:
            m = torch.nn.ZeroPad2d((0, 0, 0, p))
            result = m(result)
        elif p < 0:
            result = result[0:target_length, :]

        return result

    def _wav2fbank(self, filename, filename2=None):
        # mixup
        if filename2 == None:
            waveform, sr = torchaudio.load(filename)
            waveform = waveform - waveform.mean()
        # mixup
        else:
            waveform1, sr = torchaudio.load(filename)
            waveform2, _ = torchaudio.load(filename2)

            waveform1 = waveform1 - waveform1.mean()
            waveform2 = waveform2 - waveform2.mean()

            if waveform1.shape[1] != waveform2.shape[1]:
                if waveform1.shape[1] > waveform2.shape[1]:
                    # padding
                    temp_wav = torch.zeros(1, waveform1.shape[1])
                    temp_wav[0, 0:waveform2.shape[1]] = waveform2
                    waveform2 = temp_wav
                else:
                    # cutting
                    waveform2 = waveform2[0, 0:waveform1.shape[1]]

            # sample lambda from uniform distribution
            #mix_lambda = random.random()
            # sample lambda from beta distribtion
            mix_lambda = np.random.beta(10, 10)

            mix_waveform = mix_lambda * waveform1 + (1 - mix_lambda) * waveform2
            waveform = mix_waveform - mix_waveform.mean()

        fbank = torchaudio.compliance.kaldi.fbank(waveform, htk_compat=True, sample_frequency=sr, use_energy=False,
                                                  window_type='hanning', num_mel_bins=self.melbins, dither=0.0, frame_shift=10)

        target_length = self.audio_conf.get('target_length')
        n_frames = fbank.shape[0]

        p = target_length - n_frames

        # cut and pad
        if p > 0:
            m = torch.nn.ZeroPad2d((0, 0, 0, p))
            fbank = m(fbank)
        elif p < 0:
            fbank = fbank[0:target_length, :]

        if filename2 == None:
            return fbank, 0
        else:
            return fbank, mix_lambda

    def __getitem__(self, index):
        """
        returns: image, audio, nframes
        where image is a FloatTensor of size (3, H, W)
        audio is a FloatTensor of size (N_freq, N_frames) for spectrogram, or (N_frames) for waveform
        nframes is an integer
        """
        # do mix-up for this sample (controlled by the given mixup rate)
        if random.random() < self.mixup:
            datum = self.data[index]
            # find another sample to mix, also do balance sampling
            # sample the other sample from the multinomial distribution, will make the performance worse
            # mix_sample_idx = np.random.choice(len(self.data), p=self.sample_weight_file)
            # sample the other sample from the uniform distribution
            mix_sample_idx = random.randint(0, len(self.data)-1)
            mix_datum = self.data[mix_sample_idx]
            # get the mixed fbank
            result, mix_lambda = self._wav2fbank(datum['wav'], mix_datum['wav'])
            # initialize the label
            label_indices = np.zeros(self.label_num)
            # add sample 1 labels
            for label_str in datum['labels'].split(','):
                label_indices[int(self.index_dict[label_str])] += mix_lambda
            # add sample 2 labels
            for label_str in mix_datum['labels'].split(','):
                label_indices[int(self.index_dict[label_str])] += 1.0-mix_lambda
            label_indices = torch.FloatTensor(label_indices)
        # if not do mixup
        else:
            datum = self.data[index]
            label_indices = np.zeros(self.label_num)
            if self.mdps:
                result = self._wav2stft(datum['wav'])
            else:
                result, mix_lambda = self._wav2fbank(datum['wav'])
            for label_str in datum['labels'].split(','):
                label_indices[int(self.index_dict[label_str])] = 1.0

            label_indices = torch.FloatTensor(label_indices)

        # SpecAug, not do for eval set
        freqm = torchaudio.transforms.FrequencyMasking(self.freqm)
        timem = torchaudio.transforms.TimeMasking(self.timem)
        result = torch.transpose(result, 0, 1)
        # this is just to satisfy new torchaudio version, which only accept [1, freq, time]
        result = result.unsqueeze(0)
        if self.freqm != 0:
            result = freqm(result)
        if self.timem != 0:
            result = timem(result)
        # squeeze it back, it is just a trick to satisfy new torchaudio version
        result = result.squeeze(0)
        result = torch.transpose(result, 0, 1)

        # normalize the input for both training and test
        if not self.skip_norm:
            result = (result - self.norm_mean) / (self.norm_std * 2)
        # skip normalization the input if you are trying to get the normalization stats.
        else:
            pass

        if self.noise == True:
            result = result + torch.rand(result.shape[0], result.shape[1]) * np.random.rand() / 10
            result = torch.roll(result, np.random.randint(-10, 10), 0)

        # mix_ratio = min(mix_lambda, 1-mix_lambda) / max(mix_lambda, 1-mix_lambda)

        # the output fbank shape is [time_frame_num, frequency_bins], e.g., [1024, 128]
        return result, label_indices

    def __len__(self):
        return len(self.data)