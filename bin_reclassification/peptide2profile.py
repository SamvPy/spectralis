import numpy as np
import math
import sys
import tqdm
import itertools
import networkx as nx
from scipy.sparse import csc_matrix, csr_matrix
from collections import OrderedDict as ODict

from prosit_grpc.predictPROSIT import PROSITpredictor

from denovo_utils import __constants__ as C

import sys
import os
sys.path.insert(0, os.path.abspath('/data/nasif12/home_if12/salazar/Spectralis/bin_reclassification'))
from ms2_binning import get_binning, get_bins_assigments

class Peptide2Profile:
    
    def __init__(self, bin_resolution=1, max_mz_bin=2500, 
                 considered_ion_types=[ 'y', 'b'], considered_charges=[1,2,3],
                 path_to_ca_certificate='', path_to_key_certificate='', path_to_certificate='',
                 add_leftmost_rightmost=True,
                 verbose=False,
                 add_fragment_position=False,
                 prosit_predictor=None,
                 sqrt_transform=True,
                 log_transform=False
                ):
        '''
            bin_resolution: in [Da]
            bin lenght: default 2500
        '''
        
        self.verbose = verbose
        self.bin_resolution = bin_resolution
        self.max_mz_bin = max_mz_bin
        self.add_leftmost_rightmost = add_leftmost_rightmost
        self.add_fragment_position = add_fragment_position
        self.log_transform = log_transform
        self.sqrt_transform = sqrt_transform
        #print(self.log_transform)
        if prosit_predictor is None:
            prosit_server = 'proteomicsdb.org:8500'
            self.prosit_predictor = PROSITpredictor(server=prosit_server,
                                        path_to_ca_certificate = path_to_ca_certificate,
                                        path_to_key_certificate = path_to_key_certificate,
                                        path_to_certificate =  path_to_certificate
                                      )
        else:
            self.prosit_predictor = prosit_predictor
            
        self.considered_ion_types = considered_ion_types
        self.considered_charges = considered_charges
        
    @staticmethod
    def _compute_peptide_mass_from_seq(peptide_seq):
        if isinstance(peptide_seq, str):
            peptide_seq = peptide_seq.replace('M(ox)', 'Z').replace('M(O)', 'Z').replace('OxM', 'Z')
            return sum([C.AMINO_ACIDS_MASS[i] for i in peptide_seq]) + C.MASSES['H2O'] 
        else:
            return sum([C.VEC_MZ[i] for i in peptide_seq]) + C.MASSES['H2O']
    
    @staticmethod
    def _compute_peptide_mass(precursor_mz, z):
         return (precursor_mz - C.MASSES['PROTON'] ) * z
        
    @staticmethod
    def _subset_prosit(prosit_output, ion_type, charge):
        mzs = prosit_output['fragmentmz'].copy()
        intensities = prosit_output['intensity'].copy()
        anno = prosit_output['annotation']
        
        idx = (anno['charge']==charge) & (anno['type']==ion_type)
        mzs[~idx] = 0 # set to zero what we do not need
        intensities[~idx] = 0
        return mzs, intensities
    
    def _get_peptide2binned(self, prosit_output):
        ## Collect binned intensities for every seq and every considered fragment_type
        binned_intensities = np.zeros((len(prosit_output['fragmentmz']), #len(peptide_seqs), 
                                       math.ceil(self.max_mz_bin/self.bin_resolution), 
                                      ), dtype=np.float16)
        
        mzs = prosit_output['fragmentmz']
        intensities = prosit_output['intensity']

        ## Handle every seq
        for i in tqdm.tqdm(range(mzs.shape[0]), disable=True):
            b = get_binning(mzs[i], intensities[i],
                            max_norm=False, remove_minus_one=True, 
                            precursor_mz=None, min_intensity=0.0 ,
                            square_root=False, log_scale=False,
                            bin_resolution=self.bin_resolution,
                            max_mz_bin=self.max_mz_bin
                             ) 
            binned_intensities[i] = b
        
        return binned_intensities ## (n_seqs, n_fragment_types, n_bins)
    
    def get_peptide2binned(self, peptide_seqs, charges, ces):
        ## Get prosit preds
        prosit_output = self.prosit_predictor.predict(sequences=peptide_seqs, 
                                                      charges=charges, collision_energies=ces, 
                                                        models=["Prosit_2019_intensity"])['Prosit_2019_intensity']
    
        return self._get_peptide2binned(prosit_output)
        
        
    
    def get_peptide2binnedWchannels(self, peptide_seqs, charges, ces, sparse_representation=False):
        ## Get prosit preds
        prosit_output = self.prosit_predictor.predict(sequences=peptide_seqs, 
                                                      charges=charges, collision_energies=ces, 
                                                        models=["Prosit_2019_intensity"])['Prosit_2019_intensity']
    
        ## Collect binned intensities for every seq and every considered fragment_type
        binned_intensities = np.zeros((len(peptide_seqs), 
                                       len(self.considered_charges) * len(self.considered_ion_types),
                                       math.ceil(self.max_mz_bin/self.bin_resolution), 
                                      ), dtype=np.float16)       
        self.considered_fragments = []
        j = 0
        for charge in self.considered_charges:
            for ion_type in self.considered_ion_types:
                #print(ion_type, charge)
                self.considered_fragments.append(f'{ion_type}{"".join(["+"]*charge)}')
                ## Subset ion type and charge
                mzs, intensities = self._subset_prosit(prosit_output, ion_type=ion_type, charge=charge)
                
                ## Handle every seq
                for i in tqdm.tqdm(range(mzs.shape[0]), disable=True):
                    b = get_binning(mzs[i], intensities[i], max_norm=False, remove_minus_one=True, 
                                       precursor_mz=None, min_intensity=0.0 ,
                                       square_root=False, log_scale=False,
                                       bin_resolution=self.bin_resolution,
                                       max_mz_bin=self.max_mz_bin
                                     ) 
                    binned_intensities[i, j , :] = b
                    
                j +=1
        
        if sparse_representation==True:
            binned_intensities = [csc_matrix(binned_intensities[i], 
                                             dtype=np.float32) for i in range(binned_intensities.shape[0])  ]
        
        return binned_intensities ## (n_seqs,  n_fragment_types, n_bins)
    
    def _get_peptide2profile(self, prosit_output, sparse_representation=False, pepmass=None):
        
        ## Collect binned intensities for every seq and every considered fragment_type
        n_bins = math.ceil(self.max_mz_bin/self.bin_resolution)
        n_channels = len(self.considered_charges)*len(self.considered_ion_types)
        #if self.add_fragment_position==True:
        #    n_channels += 2
        profiles = np.zeros((len(prosit_output['fragmentmz']), #len(peptide_seqs),
                             n_channels,
                             n_bins
                            ), dtype=np.int8 )
        self.considered_fragments = []
        j = 0
        for charge in self.considered_charges:
            for ion_type in self.considered_ion_types:
                self.considered_fragments.append(f'{ion_type}{"".join(["+"]*charge)}')
                mzs, _ = self._subset_prosit(prosit_output, ion_type=ion_type, charge=charge)
                if self.verbose:
                    print(f'MZs for channel <{ion_type}{"".join(["+"]*charge)}>: {mzs}')
                ## Handle every seq
                for i in tqdm.tqdm(range(mzs.shape[0]), disable=True):                          
                            
                    current_mzs = mzs[i]
                    current_mzs = current_mzs[current_mzs>0]
                    current_mzs = np.sort(current_mzs)
                    
                    #if self.add_fragment_position==True:
                    #    fragment_positions = np.arange(len(current_mzs))+1
                        
                    if (self.add_leftmost_rightmost==True) & (pepmass is not None):
                        ### FOR B IONS: [1, mz, mass-18+1]
                        if ion_type=='b':
                            leftmost = 1.0/charge
                            rightmost = (pepmass[i] -17.0 ) / charge
                        ### FOR Y ions: [19, mz, mass-1] --> [18, mz-1, mass] --> [0, mz-19, mass-18]
                        else: #elif ion_type=='y':
                            leftmost = 19.0/charge
                            rightmost = (pepmass[i] + 1.0 ) / charge
                            
                        current_mzs = np.concatenate([[leftmost], current_mzs, [rightmost]])
                        #if self.add_fragment_position==True:
                        #    fragment_positions = np.concatenate([[0], current_mzs, [len(fragment_positions+1)]])
                    
                    #print(current_mzs)
                    assignments_idx, _ = get_bins_assigments(bin_resolution=self.bin_resolution, 
                                                                  max_mz_bin=self.max_mz_bin, 
                                                                  mzs=current_mzs)
                    assignments_idx = assignments_idx[assignments_idx>0]
                    assignments_idx = assignments_idx[assignments_idx<n_bins]
                    
                    profiles[i, j, assignments_idx ] = 1
                    
                    #if self.add_fragment_position==True:
                    #    profiles[i, j+1, sorted(assignments_idx)] = 
                        
                    
                    
                j+=1
        
        
        
        
        if sparse_representation==True:
            profiles = [csc_matrix(profiles[i], 
                                  dtype=np.int8) for i in range(profiles.shape[0])  ]          
        return profiles
    
    
    def get_peptide2profile(self, peptide_seqs, charges, ces, sparse_representation=False):
        
        ## Get prosit preds
        prosit_output = self.prosit_predictor.predict(sequences=peptide_seqs, 
                                                      charges=[int(c) for c in charges],
                                                      collision_energies=ces, 
                                                      models=["Prosit_2019_intensity"])['Prosit_2019_intensity']
        if self.add_leftmost_rightmost==True:
            pepmass = np.array([self._compute_peptide_mass_from_seq(p) for p in peptide_seqs]) 
        else:
            pepmass=None
        return self._get_peptide2profile(prosit_output, sparse_representation=sparse_representation, pepmass=pepmass)
        
        
    def get_exp2binned(self, exp_mzs, exp_int, precursor_mz=None):
        exp_binned = np.array([get_binning(exp_mzs[i], exp_int[i], 
                                    max_norm=True,
                                    remove_minus_one=False, 
                                    precursor_mz=precursor_mz[i], 
                                    min_intensity=0.0 ,
                                    square_root=False, 
                                    log_scale=False,
                                    bin_resolution=self.bin_resolution,
                                    max_mz_bin=self.max_mz_bin,
                                    ) for i in range(len(exp_mzs))], dtype=np.float16 )
        return exp_binned
    
    
    def _get_precursor_ranges(self, profiles):
        precursor_ranges = []
        for i in range(len(profiles)):
            idx_nonzero = np.where(np.sum(profiles[i], axis=0)>0)[0]
            first_nonzero = min(idx_nonzero)
            last_nonzero = max(idx_nonzero)
            
            idx = np.zeros((profiles.shape[-1],))
            idx[np.arange(first_nonzero, last_nonzero+1)] = 1
            precursor_ranges.append(idx)
            
        precursor_ranges = np.vstack(precursor_ranges)
        precursor_ranges = np.expand_dims(precursor_ranges, axis=1)
        return precursor_ranges
    
    @staticmethod
    def _apply_log_transform(x, eps=0.05):
        log_x = np.log(x+eps)
        y = (log_x-min(log_x)) / (max(log_x)-min(log_x))
        return y

    def get_exp2binned_processed(self, exp_mzs, exp_int, precursor_mz):
                
        exp_binned = self.get_exp2binned(exp_mzs, exp_int, precursor_mz)
        exp_binned = self._apply_log_transform(exp_binned, eps=0.05) if self.log_transform==True else exp_binned
        exp_binned = np.sqrt(exp_binned) if self.sqrt_transform==True else exp_binned
        exp_binned = np.expand_dims(exp_binned, axis=1)
        return exp_binned
    
    def _get_peptideWExp2binned(self,prosit_output, exp_mzs, exp_int, pepmass, precursor_mz,
                               add_intensity_diff, sparse_representation,
                               add_precursor_range
                               ):
        

        profiles = self._get_peptide2profile(prosit_output, pepmass=pepmass)
        
        # Add precursor range
        if add_precursor_range==True:
            precursor_ranges = self._get_precursor_ranges( profiles)
            profiles = np.hstack((profiles, precursor_ranges))
        
        theo_binned = self._get_peptide2binned(prosit_output)
        theo_binned = self._apply_log_transform(theo_binned, eps=0.05) if self.log_transform==True else theo_binned
        theo_binned = np.sqrt(theo_binned) if self.sqrt_transform==True else theo_binned
        theo_binned = np.expand_dims(theo_binned, axis=1)
        
        exp_binned = self.get_exp2binned_processed(exp_mzs, exp_int, precursor_mz)
        
        
        # Add intensity diff
        if add_intensity_diff==True:
            epsilon=1e-5
            max_rel_diff = 3
            diff_binned = abs(theo_binned-exp_binned)
            
            rel_diff_binned = diff_binned/((theo_binned+exp_binned)/2.0+epsilon)
            #rel_diff_binned[rel_diff_binned>max_rel_diff] = max_rel_diff
            #rell_diff_binned = rel_diff_binned/max_rel_diff
            
            combined = np.hstack((profiles, diff_binned, rel_diff_binned, theo_binned, exp_binned))
        else:
            combined = np.hstack((profiles, theo_binned, exp_binned))
        
        
        
        if sparse_representation==True:
            combined = [csc_matrix(combined[i], 
                                    dtype=np.float32) for i in range(combined.shape[0])  ]
        
        return combined
        
    
    
    def get_peptideWExp2binned(self, peptide_seqs, charges, ces, 
                               exp_mzs, exp_int, precursor_mz, sparse_representation=False,
                               add_intensity_diff=False, add_precursor_range=False, 
                              ):
        
        if self.add_leftmost_rightmost==True:
            pepmass = np.array([self._compute_peptide_mass_from_seq(p) for p in peptide_seqs]) 
        else:
            pepmass = None
            
        prosit_output = self.prosit_predictor.predict(sequences=peptide_seqs, 
                                                      charges=charges, collision_energies=ces, 
                                                      models=["Prosit_2019_intensity"])['Prosit_2019_intensity']
        return self._get_peptideWExp2binned(prosit_output=prosit_output, 
                                            exp_mzs=exp_mzs,
                                            exp_int=exp_int,
                                            pepmass=pepmass,
                                            precursor_mz=precursor_mz,
                                            add_intensity_diff=add_intensity_diff, 
                                            sparse_representation=sparse_representation,
                                            add_precursor_range=add_precursor_range
                                           )
        
    
    def get_peptideWExp2binnedAcrossChannels(self, peptide_seqs, charges, ces, 
                               exp_mzs, exp_int, precursor_mz, sparse_representation=False):
        
        binned_int = self.get_peptide2binnedWchannels(peptide_seqs, charges, ces)
        exp_binned_int = self.get_exp2binned(exp_mzs, exp_int, precursor_mz)
        
        exp_binned_int = np.expand_dims(exp_binned_int, axis=1)
        exp_binned_int = np.hstack((binned_int,exp_binned_int))
        
        if sparse_representation==True:
            exp_binned_int = [csc_matrix(exp_binned_int[i], 
                                         dtype=np.float32) for i in range(exp_binned_int.shape[0])  ]
        
        return exp_binned_int


    
