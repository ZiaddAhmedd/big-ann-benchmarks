import pdb
import pickle
import numpy as np
import os
import gc
import random
from scipy.sparse import csr_matrix, tril, lil_matrix, save_npz, load_npz
from multiprocessing.pool import ThreadPool

import faiss

from neurips23.filter.base import BaseFilterANN
from benchmark.datasets import DATASETS
from benchmark.dataset_io import download_accelerated

import bow_id_selector

def csr_get_row_indices(m, i):
    """ get the non-0 column indices for row i in matrix m """
    return m.indices[m.indptr[i] : m.indptr[i + 1]]

def make_bow_id_selector(mat, id_mask=0):
    sp = faiss.swig_ptr
    if id_mask == 0:
        return bow_id_selector.IDSelectorBOW(mat.shape[0], sp(mat.indptr), sp(mat.indices))
    else:
        return bow_id_selector.IDSelectorBOWBin(
            mat.shape[0], sp(mat.indptr), sp(mat.indices), id_mask
        )

def set_invlist_ids(invlists, l, ids):
    n, = ids.shape
    ids = np.ascontiguousarray(ids, dtype='int64')
    assert invlists.list_size(l) == n
    faiss.memcpy(
        invlists.get_ids(l),
        faiss.swig_ptr(ids), n * 8
    )



def csr_to_bitcodes(matrix, bitsig):
    """ Compute binary codes for the rows of the matrix: each binary code is
    the OR of bitsig for non-0 entries of the row.
    """
    indptr = matrix.indptr
    indices = matrix.indices
    n = matrix.shape[0]
    bit_codes = np.zeros(n, dtype='int64')
    for i in range(n):
        # print(bitsig[indices[indptr[i]:indptr[i + 1]]])
        bit_codes[i] = np.bitwise_or.reduce(bitsig[indices[indptr[i]:indptr[i + 1]]])
    return bit_codes


class BinarySignatures:
    """ binary signatures that encode vectors """

    def __init__(self, metadata):
        nvec, nword = metadata.shape
        # number of bits reserved for the vector ids
        self.id_bits = int(np.ceil(np.log2(nvec)))
        # number of bits for the binary signature
        self.sig_bits = nbits = 63 - self.id_bits

        # select binary signatures for the vocabulary
        rs = np.random.RandomState(123)    # we rely on this to be reproducible!
        
        temp = np.full((nword, nbits), False, dtype=bool)
        initial_step = 1024
        random.seed(123)
        step = initial_step
        words = [i for i in range(nword)]
        index = 0
        count = 0
        SetBits = np.zeros(nvec, dtype=int)
        TempSetBits = np.zeros(nvec, dtype=int)
        SetWords = set()
        while index < nbits:
            #print(index, step, np.sum(TempSetBits))
            if count + step > metadata.shape[1]:
                step = int(metadata.shape[1] - count)
            if count % metadata.shape[1] == 0:
                random.shuffle(words)
                count = 0
                step = initial_step
            bits = metadata[:,words[count:count+step]].nonzero()[0]
            TempSetBits[bits] = 1 
            if np.sum(TempSetBits) < metadata.shape[0] / 2:
                SetBits = np.copy(TempSetBits)
                SetWords = SetWords.union(words[count:count+step])
                count += step
            else:
                if step > 1:
                    step = int(step/2) 
                    TempSetBits = np.copy(SetBits)
                else:
                    for w in SetWords:
                        temp[w, index] = True
                    SetBits = np.zeros(metadata.shape[0], dtype=int)
                    TempSetBits = np.zeros(metadata.shape[0], dtype=int)
                    SetWords = set()
                    index += 1
                    step = initial_step
        
        bitsig = np.packbits(temp, axis=1)
        #bitsig = np.packbits(rs.rand(nword, nbits) < proba_1, axis=1)
        bitsig = np.pad(bitsig, ((0, 0), (0, 8 - bitsig.shape[1]))).view("int64").ravel()
        self.bitsig = bitsig

        # signatures for all the metadata matrix
        self.db_sig = csr_to_bitcodes(metadata, bitsig) << self.id_bits

        # mask to keep only the ids
        self.id_mask = (1 << self.id_bits) - 1

    def query_signature(self, w1, w2):
        """ compute the query signature for 1 or 2 words """
        sig = self.bitsig[w1]
        if w2 != -1:
            sig |= self.bitsig[w2]
        return int(sig << self.id_bits)

class Tuned_FAISS(BaseFilterANN):

    def __init__(self,  metric, index_params):
        self._index_params = index_params
        self._metric = metric
        print(index_params)
        self.indexkey = index_params.get("indexkey", "IVF32768,SQ8")
        self.binarysig = index_params.get("binarysig", True)
        self.metadata_threshold = 1e-3
        self.nt = index_params.get("threads", 1)
    

    def construct_frequency_matrix(self, metadata):
        max_th = 100000 # 0.01 * meta_b.shape[0]
        nvec, nword = metadata.shape
        self.frequency_matrix = lil_matrix((nword,nword),dtype=np.uint32)
        for i in range(100):
            begin = int(i * nvec / 100)
            end = int((i + 1) * nvec / 100)    
            self.frequency_matrix = self.frequency_matrix + (metadata[begin:end,:].transpose() @ metadata[begin:end,:]).tolil()
    
        self.frequency_matrix = tril(self.frequency_matrix.tocsr()).tocsr()
        self.frequency_matrix.data[self.frequency_matrix.data > max_th] = 0
        self.frequency_matrix.data[self.frequency_matrix.data < 10] = 0
        self.frequency_matrix.eliminate_zeros()
        gc.collect()
        

    def fit(self, dataset):
        ds = DATASETS[dataset]()
        if ds.search_type() == "knn_filtered" and self.binarysig:
            print("preparing binary signatures")
            meta_b = ds.get_dataset_metadata()
            # Constructing the frequency matrix
            self.construct_frequency_matrix(meta_b)
            save_npz(self.frequency_matrix_name(dataset), self.frequency_matrix)
            
            self.binsig = BinarySignatures(meta_b)
            print("writing to", self.binarysig_name(dataset))
            pickle.dump(self.binsig, open(self.binarysig_name(dataset), "wb"), -1)
        else:
            self.binsig = None

        if ds.search_type() == "knn_filtered":
            self.meta_b = ds.get_dataset_metadata()
            self.meta_b.sort_indices()

        index = faiss.index_factory(ds.d, self.indexkey)
        xb = ds.get_dataset()
        print("train")
        index.train(xb)
        print("populate")
        if self.binsig is None:
            index.add(xb)
        else:
            ids = np.arange(ds.nb) | self.binsig.db_sig
            index.add_with_ids(xb, ids)

        self.index = index
        self.nb = ds.nb
        self.xb = xb
        self.ps = faiss.ParameterSpace()
        self.ps.initialize(self.index)
        print("store", self.index_name(dataset))
        faiss.write_index(index, self.index_name(dataset))

    
    def index_name(self, name):
        return f"data/{name}.{self.indexkey}.faissindex"
    
    def binarysig_name(self, name):
        return f"data/{name}.{self.indexkey}.binarysig"
        
    def frequency_matrix_name(self, name):
        return f"data/{name}.{self.indexkey}.frequency_matrix.npz"


    def load_index(self, dataset):
        """
        Load the index for dataset. Returns False if index
        is not available, True otherwise.

        Checking the index usually involves the dataset name
        and the index build paramters passed during construction.
        """
        if not os.path.exists(self.index_name(dataset)):
            if 'url' not in self._index_params:
                return False

            print('Downloading index in background. This can take a while.')
            download_accelerated(self._index_params['url'], self.index_name(dataset), quiet=True)

        print("Loading index")

        self.index = faiss.read_index(self.index_name(dataset))

        self.ps = faiss.ParameterSpace()
        self.ps.initialize(self.index)

        ds = DATASETS[dataset]()

        if ds.search_type() == "knn_filtered" and self.binarysig:
            if not os.path.exists(self.frequency_matrix_name(dataset)):
                print("preparing frequecy matrix")
                meta_b = ds.get_dataset_metadata()
                self.construct_frequency_matrix(meta_b)
            else:
                print("loading frequency matrix")
                self.frequency_matrix = load_npz(self.frequency_matrix_name(dataset))
            if not os.path.exists(self.binarysig_name(dataset)):
                print("preparing binary signatures")
                meta_b = ds.get_dataset_metadata()
                self.binsig = BinarySignatures(meta_b)
            else:
                print("loading binary signatures")
                self.binsig = pickle.load(open(self.binarysig_name(dataset), "rb"))
        else:
            self.binsig = None

        if ds.search_type() == "knn_filtered":
            self.meta_b = ds.get_dataset_metadata()
            self.meta_b.sort_indices()

        self.nb = ds.nb
        self.xb = ds.get_dataset()

        return True        

    def index_files_to_store(self, dataset):
        """
        Specify a triplet with the local directory path of index files,
        the common prefix name of index component(s) and a list of
        index components that need to be uploaded to (after build)
        or downloaded from (for search) cloud storage.

        For local directory path under docker environment, please use
        a directory under
        data/indices/track(T1 or T2)/algo.__str__()/DATASETS[dataset]().short_name()
        """
        raise NotImplementedError()
    
    def query(self, X, k):
        nq = X.shape[0]
        self.I = -np.ones((nq, k), dtype='int32')        
        bs = 1024
        for i0 in range(0, nq, bs):
            _, self.I[i0:i0+bs] = self.index.search(X[i0:i0+bs], k)

    
    def filtered_query(self, X, filter, k):
        print('running filtered query')
        nq = X.shape[0]
        self.I = -np.ones((nq, k), dtype='int32')
        meta_b = self.meta_b
        frequency_matrix = self.frequency_matrix
        meta_q = filter
        docs_per_word = meta_b.T.tocsr()
        threshold = self.metadata_threshold * self.nb
        
        def process_one_row(q):
            faiss.omp_set_num_threads(1)
            qwords = csr_get_row_indices(meta_q, q)
            assert qwords.size in (1, 2)
            w1 = qwords[0]
            freq = 0
            if qwords.size == 2:
                w2 = qwords[1]
                if w1 > w2:
                    freq = frequency_matrix[w1, w2]
                else:
                    freq = frequency_matrix[w2, w1]
            else:
                w2 = -1
                freq = frequency_matrix[w1, w1]
            if freq < threshold and frequency >= k:
                # metadata first
                docs = csr_get_row_indices(docs_per_word, w1)
                if w2 != -1:
                    docs = bow_id_selector.intersect_sorted(
                        docs, csr_get_row_indices(docs_per_word, w2))

                assert len(docs) >= k, pdb.set_trace()
                xb_subset = self.xb[docs]
                _, Ii = faiss.knn(X[q : q + 1], xb_subset, k=k)
 
                self.I[q, :] = docs[Ii.ravel()]
            else:
                # IVF first, filtered search
                sel = make_bow_id_selector(meta_b, self.binsig.id_mask if self.binsig else 0)
                if self.binsig is None:
                    sel.set_query_words(int(w1), int(w2))
                else:
                    sel.set_query_words_mask(
                        int(w1), int(w2), self.binsig.query_signature(w1, w2))

                params = faiss.SearchParametersIVF(sel=sel, nprobe=self.nprobe)

                _, Ii = self.index.search(
                    X[q:q+1], k, params=params
                )
                Ii = Ii.ravel()
                if self.binsig is None:
                    self.I[q] = Ii
                else:
                    # we'll just assume there are enough results
                    # valid = Ii != -1
                    # I[q, valid] = Ii[valid] & binsig.id_mask
                    self.I[q] = Ii & self.binsig.id_mask


        if self.nt <= 1:
            for q in range(nq):
                process_one_row(q)
        else:
            faiss.omp_set_num_threads(self.nt)
            pool = ThreadPool(self.nt)
            list(pool.map(process_one_row, range(nq)))

    def get_results(self):
        return self.I

    def set_query_arguments(self, query_args):
        faiss.cvar.indexIVF_stats.reset()
        if "nprobe" in query_args:
            self.nprobe = query_args['nprobe']
            self.ps.set_index_parameters(self.index, f"nprobe={query_args['nprobe']}")
            self.qas = query_args
        else:
            self.nprobe = 1
        if "mt_threshold" in query_args:
            self.metadata_threshold = query_args['mt_threshold']
        else:
            self.metadata_threshold = 1e-3

    def __str__(self):
        return f'Faiss({self.indexkey, self.qas})'

   
