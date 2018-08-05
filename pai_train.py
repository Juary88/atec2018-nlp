#/usr/bin/env python
#coding=utf-8

import multiprocessing
import os
import re
import gensim
import jieba
import keras
import keras.backend as K
import numpy as np
import pandas as pd
from itertools import combinations
from keras.activations import softmax
from keras.callbacks import EarlyStopping, ModelCheckpoint,LambdaCallback, Callback, ReduceLROnPlateau, LearningRateScheduler
from keras.layers import *
from keras.models import Model
from keras.optimizers import SGD, Adadelta, Adam, Nadam, RMSprop
from keras.regularizers import L1L2, l2
from keras.preprocessing.sequence import pad_sequences
from keras.engine.topology import Layer
#from keras import initializations
from keras import initializers, regularizers, constraints

from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import f1_score
from sklearn.model_selection import train_test_split, KFold

os.environ["TF_CPP_MIN_LOG_LEVEL"]='3'

online = True
if not online:
    model_dir = "pai_model/"
    test_size = 0.05
else:
    test_size = 0.025

fast_mode, fast_rate = False,0.15    # 快速调试
random_state = 42
MAX_LEN = 30
MAX_EPOCH = 90
batch_size = 2000
w2v_length = 256
earlystop_patience, plateau_patience = 8,2    # patience
cfgs = [
    ("siamese", "char", 24, w2v_length,    [100, 80, 64, 64],   102-5, earlystop_patience),  # 69s
    ("siamese", "word", 20, w2v_length,    [100, 80, 64, 64],   120-4, earlystop_patience),  # 59s
    ("esim",    "char", 24, w2v_length,    [],             18,  earlystop_patience),  # 389s
    ("esim",    "word", 20, w2v_length,    [],             21,  earlystop_patience),  # 335s   
    ("decom",   "char", 24, w2v_length,    [],             87-2,  earlystop_patience),   # 84s
    ("decom",   "word", 20, w2v_length,    [],             104-4, earlystop_patience),  # 71s
    ("dssm",    "both", [20,24], w2v_length, [],           124-8, earlystop_patience), # 55s
]
weights = [model_dir + "%s_%s.h5"%(cfg[0],cfg[1]) for cfg in cfgs]
final_weights = [model_dir + "%s_%s_all.h5"%(cfg[0],cfg[1]) for cfg in cfgs]

combines, num_models = [],len(cfgs)
for i in range(1,num_models+1):
    combines.extend([list(c) for c in combinations(range(num_models), i)])

new_words = "支付宝 付款码 二维码 收钱码 转账 退款 退钱 余额宝 运费险 还钱 还款 花呗 借呗 蚂蚁花呗 蚂蚁借呗 蚂蚁森林 小黄车 飞猪 微客 宝卡 芝麻信用 亲密付 淘票票 饿了么 摩拜 滴滴 滴滴出行".split(" ")
for word in new_words:
    jieba.add_word(word)

star = re.compile("\*+")

ebed_type = "gensim"

if ebed_type == "gensim":
    char_embedding_model = gensim.models.Word2Vec.load(model_dir + "char2vec_gensim%s"%w2v_length)
    char2index = {v:k for k,v in enumerate(char_embedding_model.wv.index2word)}
    word_embedding_model = gensim.models.Word2Vec.load(model_dir + "word2vec_gensim%s"%w2v_length)
    word2index = {v:k for k,v in enumerate(word_embedding_model.wv.index2word)}

elif ebed_type == "fastskip" or ebed_type == "fastcbow":
    char_embedding_matrix = np.load(model_dir + "fasttext/char2vec_%s%d.npy"%(ebed_type, w2v_length))
    char2index = {line.split()[0]:int(line.split()[1]) for line in open(model_dir + "fasttext/char2index_%s%d.csv"%(ebed_type, w2v_length),'r',encoding='utf8')}
    word_embedding_matrix = np.load(model_dir + "fasttext/word2vec_%s%d.npy"%(ebed_type,w2v_length))
    word2index = {line.split()[0]:int(line.split()[1]) for line in open(model_dir + "fasttext/word2index_%s%d.csv"%(ebed_type, w2v_length),'r',encoding='utf8')}

print("loaded w2v done!", len(char2index), len(word2index))


def create_pretrained_embedding(pretrained_weights_path, trainable=False, **kwargs):
    "Create embedding layer from a pretrained weights array"
    pretrained_weights = np.load(pretrained_weights_path)
    in_dim, out_dim = pretrained_weights.shape
    embedding = Embedding(in_dim, out_dim, weights=[pretrained_weights], trainable=False, **kwargs)
    return embedding


def unchanged_shape(input_shape):
    "Function for Lambda layer"
    return input_shape


def substract(input_1, input_2):
    "Substract element-wise"
    neg_input_2 = Lambda(lambda x: -x, output_shape=unchanged_shape)(input_2)
    out_ = Add()([input_1, neg_input_2])
    return out_


def submult(input_1, input_2):
    "Get multiplication and subtraction then concatenate results"
    mult = Multiply()([input_1, input_2])
    sub = substract(input_1, input_2)
    out_= Concatenate()([sub, mult])
    return out_


def apply_multiple(input_, layers):
    "Apply layers to input then concatenate result"
    if not len(layers) > 1:
        raise ValueError('Layers list should contain more than 1 layer')
    else:
        agg_ = []
        for layer in layers:
            agg_.append(layer(input_))
        out_ = Concatenate()(agg_)
    return out_


def time_distributed(input_, layers):
    "Apply a list of layers in TimeDistributed mode"
    out_ = []
    node_ = input_
    for layer_ in layers:
        node_ = TimeDistributed(layer_)(node_)
    out_ = node_
    return out_


def soft_attention_alignment(input_1, input_2):
    "Align text representation with neural soft attention"
    attention = Dot(axes=-1)([input_1, input_2])
    w_att_1 = Lambda(lambda x: softmax(x, axis=1),
                     output_shape=unchanged_shape)(attention)
    w_att_2 = Permute((2,1))(Lambda(lambda x: softmax(x, axis=2),
                             output_shape=unchanged_shape)(attention))
    in1_aligned = Dot(axes=1)([w_att_1, input_1])
    in2_aligned = Dot(axes=1)([w_att_2, input_2])
    return in1_aligned, in2_aligned

def decomposable_attention(pretrained_embedding='../data/fasttext_matrix.npy', 
                           projection_dim=300, projection_hidden=0, projection_dropout=0.2,
                           compare_dim=500, compare_dropout=0.2,
                           dense_dim=300, dense_dropout=0.2,
                           lr=1e-3, activation='elu', maxlen=MAX_LEN):
    # Based on: https://arxiv.org/abs/1606.01933
    
    q1 = Input(name='q1',shape=(maxlen,))
    q2 = Input(name='q2',shape=(maxlen,))
    
    # Embedding
    # embedding = create_pretrained_embedding(pretrained_embedding, 
    #                                         mask_zero=False)
    embedding = pretrained_embedding
    q1_embed = embedding(q1)
    q2_embed = embedding(q2)
    
    # Projection
    projection_layers = []
    if projection_hidden > 0:
        projection_layers.extend([
                Dense(projection_hidden, activation=activation),
                Dropout(rate=projection_dropout),
            ])
    projection_layers.extend([
            Dense(projection_dim, activation=None),
            Dropout(rate=projection_dropout),
        ])
    q1_encoded = time_distributed(q1_embed, projection_layers)
    q2_encoded = time_distributed(q2_embed, projection_layers)
    
    # Attention
    q1_aligned, q2_aligned = soft_attention_alignment(q1_encoded, q2_encoded)    
    
    # Compare
    q1_combined = Concatenate()([q1_encoded, q2_aligned, submult(q1_encoded, q2_aligned)])
    q2_combined = Concatenate()([q2_encoded, q1_aligned, submult(q2_encoded, q1_aligned)]) 
    compare_layers = [
        Dense(compare_dim, activation=activation),
        Dropout(compare_dropout),
        Dense(compare_dim, activation=activation),
        Dropout(compare_dropout),
    ]
    q1_compare = time_distributed(q1_combined, compare_layers)
    q2_compare = time_distributed(q2_combined, compare_layers)
    
    # Aggregate
    q1_rep = apply_multiple(q1_compare, [GlobalAvgPool1D(), GlobalMaxPool1D()])
    q2_rep = apply_multiple(q2_compare, [GlobalAvgPool1D(), GlobalMaxPool1D()])
    
    # Classifier
    merged = Concatenate()([q1_rep, q2_rep])
    dense = BatchNormalization()(merged)
    dense = Dense(dense_dim, activation=activation)(dense)
    dense = Dropout(dense_dropout)(dense)
    dense = BatchNormalization()(dense)
    dense = Dense(dense_dim, activation=activation)(dense)
    dense = Dropout(dense_dropout)(dense)
    out_ = Dense(1, activation='sigmoid')(dense)
    
    model = Model(inputs=[q1, q2], outputs=out_)
    return model


def esim(pretrained_embedding='../data/fasttext_matrix.npy', 
         maxlen=MAX_LEN, 
         lstm_dim=300, 
         dense_dim=300, 
         dense_dropout=0.5):
             
    # Based on arXiv:1609.06038
    q1 = Input(name='q1',shape=(maxlen,))
    q2 = Input(name='q2',shape=(maxlen,))
    
    # Embedding
    # embedding = create_pretrained_embedding(pretrained_embedding, mask_zero=False)
    embedding = pretrained_embedding
    bn = BatchNormalization(axis=2)
    q1_embed = bn(embedding(q1))
    q2_embed = bn(embedding(q2))

    # Encode
    encode = Bidirectional(CuDNNLSTM(lstm_dim, return_sequences=True))
    q1_encoded = encode(q1_embed)
    q2_encoded = encode(q2_embed)
    
    # Attention
    q1_aligned, q2_aligned = soft_attention_alignment(q1_encoded, q2_encoded)
    
    # Compose
    q1_combined = Concatenate()([q1_encoded, q2_aligned, submult(q1_encoded, q2_aligned)])
    q2_combined = Concatenate()([q2_encoded, q1_aligned, submult(q2_encoded, q1_aligned)]) 
       
    compose = Bidirectional(CuDNNLSTM(lstm_dim, return_sequences=True))
    q1_compare = compose(q1_combined)
    q2_compare = compose(q2_combined)
    
    # Aggregate
    q1_rep = apply_multiple(q1_compare, [GlobalAvgPool1D(), GlobalMaxPool1D()])
    q2_rep = apply_multiple(q2_compare, [GlobalAvgPool1D(), GlobalMaxPool1D()])
    
    # Classifier
    merged = Concatenate()([q1_rep, q2_rep])
    
    dense = BatchNormalization()(merged)
    dense = Dense(dense_dim, activation='elu')(dense)
    dense = BatchNormalization()(dense)
    dense = Dropout(dense_dropout)(dense)
    dense = Dense(dense_dim, activation='elu')(dense)
    dense = BatchNormalization()(dense)
    dense = Dropout(dense_dropout)(dense)
    out_ = Dense(1, activation='sigmoid')(dense)
    
    model = Model(inputs=[q1, q2], outputs=out_)
    return model

def custom_loss(y_true, y_pred):
    margin = 1
    return K.mean(0.25 * y_true * K.square(1 - y_pred) +
                (1 - y_true) * K.square(K.maximum(y_pred, 0)))

def siamese(pretrained_embedding=None,
            input_length=MAX_LEN, 
            w2v_length=300, 
            n_hidden=[64, 64, 64]):
    #输入层
    left_input = Input(shape=(input_length,), dtype='int32')
    right_input = Input(shape=(input_length,), dtype='int32')

    #对句子embedding
    encoded_left = pretrained_embedding(left_input)
    encoded_right = pretrained_embedding(right_input)

    #两个LSTM共享参数
    # # v1 一层lstm
    # shared_lstm = CuDNNLSTM(n_hidden)

    # # v2 带drop和正则化的多层lstm
    ipt = Input(shape=(input_length, w2v_length))
    dropout_rate = 0.5
    x = Dropout(dropout_rate, )(ipt)
    for i,hidden_length in enumerate(n_hidden):
        # x = Bidirectional(CuDNNLSTM(hidden_length, return_sequences=(i!=len(n_hidden)-1), kernel_regularizer=L1L2(l1=0.01, l2=0.01)))(x)
        x = Bidirectional(CuDNNLSTM(hidden_length, return_sequences=True, kernel_regularizer=L1L2(l1=0.01, l2=0.01)))(x)

    # v3 卷及网络特征层
    x = Conv1D(64, kernel_size = 2, strides = 1, padding = "valid", kernel_initializer = "he_uniform")(x)
    x_p1 = GlobalAveragePooling1D()(x)
    x_p2 = GlobalMaxPooling1D()(x)
    x = Concatenate()([x_p1, x_p2])
    shared_lstm = Model(inputs=ipt, outputs=x)

    left_output = shared_lstm(encoded_left)
    right_output = shared_lstm(encoded_right)


    # 距离函数 exponent_neg_manhattan_distance
    malstm_distance = Lambda(lambda x: K.exp(-K.sum(K.abs(x[0] - x[1]), axis=1, keepdims=True)),
                            output_shape=lambda x: (x[0][0], 1))([left_output, right_output])

    model = Model([left_input, right_input], [malstm_distance])

    return model

class Attention(Layer):
    def __init__(self, step_dim,
                 W_regularizer=None, b_regularizer=None,
                 W_constraint=None, b_constraint=None,
                 bias=True, **kwargs):
        """
        Keras Layer that implements an Attention mechanism for temporal data.
        Supports Masking.
        Follows the work of Raffel et al. [https://arxiv.org/abs/1512.08756]
        # Input shape
            3D tensor with shape: `(samples, steps, features)`.
        # Output shape
            2D tensor with shape: `(samples, features)`.
        :param kwargs:
        Just put it on top of an RNN Layer (GRU/LSTM/SimpleRNN) with return_sequences=True.
        The dimensions are inferred based on the output shape of the RNN.
        Example:
            model.add(LSTM(64, return_sequences=True))
            model.add(Attention())
        """
        self.supports_masking = True
        #self.init = initializations.get('glorot_uniform')
        self.init = initializers.get('glorot_uniform')

        self.W_regularizer = regularizers.get(W_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)

        self.W_constraint = constraints.get(W_constraint)
        self.b_constraint = constraints.get(b_constraint)

        self.bias = bias
        self.step_dim = step_dim
        self.features_dim = 0
        super(Attention, self).__init__(**kwargs)

    def build(self, input_shape):
        assert len(input_shape) == 3

        self.W = self.add_weight(shape=(input_shape[-1],),
                                 initializer=self.init,
                                 name='%s_W'%self.name,
                                 regularizer=self.W_regularizer,
                                 constraint=self.W_constraint)
        self.features_dim = input_shape[-1]

        if self.bias:
            self.b = self.add_weight(shape=(input_shape[1],),
                                     initializer='zero',
                                     name='%s_b'%self.name,
                                     regularizer=self.b_regularizer,
                                     constraint=self.b_constraint)
        else:
            self.b = None

        self.built = True

    def compute_mask(self, input, input_mask=None):
        # do not pass the mask to the next layers
        return None

    def call(self, x, mask=None):
        # eij = K.dot(x, self.W) TF backend doesn't support it

        # features_dim = self.W.shape[0]
        # step_dim = x._keras_shape[1]

        features_dim = self.features_dim
        step_dim = self.step_dim
        eij = K.reshape(K.dot(K.reshape(x, (-1, features_dim)), K.reshape(self.W, (features_dim, 1))), (-1, step_dim))

        if self.bias:
            eij += self.b

        eij = K.tanh(eij)
        a = K.exp(eij)
        # apply mask after the exp. will be re-normalized next
        if mask is not None:
            # Cast the mask to floatX to avoid float64 upcasting in theano
            a *= K.cast(mask, K.floatx())

        # in some cases especially in the early stages of training the sum may be almost zero
        a /= K.cast(K.sum(a, axis=1, keepdims=True) + K.epsilon(), K.floatx())

        a = K.expand_dims(a)
        weighted_input = x * a
        #print weigthted_input.shape
        return K.sum(weighted_input, axis=1)

    def compute_output_shape(self, input_shape):
        #return input_shape[0], input_shape[-1]
        return input_shape[0],  self.features_dim
        

def DSSM(pretrained_embedding, input_length, lstmsize=90):
    word_embedding, char_embedding = pretrained_embedding
    wordlen, charlen = input_length

    input1 = Input(shape=(wordlen,))
    input2 = Input(shape=(wordlen,))
    lstm0 = CuDNNLSTM(lstmsize,return_sequences = True)
    lstm1 = Bidirectional(CuDNNLSTM(lstmsize))
    lstm2 = CuDNNLSTM(lstmsize)
    att1 = Attention(wordlen)
    den = Dense(64,activation = 'tanh')

    # att1 = Lambda(lambda x: K.max(x,axis = 1))

    v1 = word_embedding(input1)
    v2 = word_embedding(input2)
    v11 = lstm1(v1)
    v22 = lstm1(v2)
    v1ls = lstm2(lstm0(v1))
    v2ls = lstm2(lstm0(v2))
    v1 = Concatenate(axis=1)([att1(v1),v11])
    v2 = Concatenate(axis=1)([att1(v2),v22])

    input1c = Input(shape=(charlen,))
    input2c = Input(shape=(charlen,))
    lstm1c = Bidirectional(CuDNNLSTM(lstmsize))
    att1c = Attention(charlen)
    v1c = char_embedding(input1c)
    v2c = char_embedding(input2c)
    v11c = lstm1c(v1c)
    v22c = lstm1c(v2c)
    v1c = Concatenate(axis=1)([att1c(v1c),v11c])
    v2c = Concatenate(axis=1)([att1c(v2c),v22c])


    mul = Multiply()([v1,v2])
    sub = Lambda(lambda x: K.abs(x))(Subtract()([v1,v2]))
    maximum = Maximum()([Multiply()([v1,v1]),Multiply()([v2,v2])])
    mulc = Multiply()([v1c,v2c])
    subc = Lambda(lambda x: K.abs(x))(Subtract()([v1c,v2c]))
    maximumc = Maximum()([Multiply()([v1c,v1c]),Multiply()([v2c,v2c])])
    sub2 = Lambda(lambda x: K.abs(x))(Subtract()([v1ls,v2ls]))
    matchlist = Concatenate(axis=1)([mul,sub,mulc,subc,maximum,maximumc,sub2])
    matchlist = Dropout(0.05)(matchlist)

    matchlist = Concatenate(axis=1)([Dense(32,activation = 'relu')(matchlist),Dense(48,activation = 'sigmoid')(matchlist)])
    res = Dense(1, activation = 'sigmoid')(matchlist)


    model = Model(inputs=[input1, input2, input1c, input2c], outputs=res)
    return model
    
def load_data(dtype = "both", input_length=[20,24], w2v_length=300):

    def __load_data(dtype = "word", input_length=20, w2v_length=300):

        filename = model_dir+"%s_%d_%d"%(dtype, input_length, w2v_length)
        if os.path.exists(filename):
            return pd.read_pickle(filename)

        data_l_n = []
        data_r_n = []
        y = []
        for line in open(model_dir+"atec_nlp_sim_train.csv","r", encoding="utf8"):
            lineno, s1, s2, label=line.strip().split("\t")
            if dtype == "word":
                data_l_n.append([word2index[word] for word in list(jieba.cut(star.sub("1",s1))) if word in word2index]) 
                data_r_n.append([word2index[word] for word in list(jieba.cut(star.sub("1",s2))) if word in word2index])
            if dtype == "char":
                data_l_n.append([char2index[char] for char in s1 if char in char2index]) 
                data_r_n.append([char2index[char] for char in s2 if char in char2index])

            y.append(int(label))

        # 对齐语料中句子的长度 
        data_l_n = pad_sequences(data_l_n, maxlen=input_length)
        data_r_n = pad_sequences(data_r_n, maxlen=input_length)
        y = np.array(y)

        pd.to_pickle((data_l_n, data_r_n, y), filename)

        return (data_l_n, data_r_n, y)

    if dtype == "both":
        ret_array = []
        for dtype,input_length in zip(['word', 'char'],input_length):
            data_l_n,data_r_n,y = __load_data(dtype, input_length, w2v_length)
            ret_array.append(np.asarray(data_l_n))
            ret_array.append(np.asarray(data_r_n))
        ret_array.append(y)
        return ret_array
    else:
        return __load_data(dtype, input_length, w2v_length)

def input_data(sent1, sent2, dtype = "both", input_length=[20,24]):
    def __input_data(sent1, sent2, dtype = "word", input_length=20):
        data_l_n = []
        data_r_n = []
        for s1, s2 in zip(sent1, sent2):
            if dtype == "word":
                data_l_n.append([word2index[word] for word in list(jieba.cut(star.sub("1",s1))) if word in word2index]) 
                data_r_n.append([word2index[word] for word in list(jieba.cut(star.sub("1",s2))) if word in word2index])
            if dtype == "char":
                data_l_n.append([char2index[char] for char in s1 if char in char2index]) 
                data_r_n.append([char2index[char] for char in s2 if char in char2index])

        # 对齐语料中句子的长度 
        data_l_n = pad_sequences(data_l_n, maxlen=input_length)
        data_r_n = pad_sequences(data_r_n, maxlen=input_length)

        return [data_l_n, data_r_n]

    if dtype == "both":
        ret_array = []
        for dtype,input_length in zip(['word', 'char'],input_length):
            data_l_n,data_r_n = __input_data(sent1, sent2, dtype, input_length)
            ret_array.append(data_l_n)
            ret_array.append(data_r_n)
        return ret_array
    else:
        return __input_data(sent1, sent2, dtype, input_length)

def split_data(data,mode="train", test_size=test_size, random_state=random_state):
    # mode == "train":  划分成用于训练的四元组
    # mode == "orig":   划分成两组数据
    train = []
    test = []
    for data_i in data:
        if fast_mode:
            data_i, _ = train_test_split(data_i,test_size=1-fast_rate,random_state=random_state )
        train_data, test_data = train_test_split(data_i,test_size=test_size,random_state=random_state )
        train.append(np.asarray(train_data))
        test.append(np.asarray(test_data))

    if mode == "orig":
        return train, test

    train_x, train_y, test_x, test_y = train[:-1], train[-1], test[:-1], test[-1]
    return train_x, train_y, test_x, test_y

def split_data_index(data, train_index, test_index):
    if len(data) == 3:
        data_l_n,data_r_n,y = data
        train_x = [data_l_n[train_index], data_r_n[train_index]]
        train_y = y[train_index]
        test_x = [data_l_n[test_index], data_r_n[test_index]]
        test_y = y[test_index]
    elif len(data) == 5:
        train = []
        test = []
        for data_i in data:
            train.append(data_i[train_index])
            test.append(data_i[test_index])
        train_x, train_y, test_x, test_y = train[:4], train[4], test[:4], test[4]
    return train_x, train_y, test_x, test_y

def r_f1_thresh(y_pred,y_true):
    e = np.zeros((len(y_true),2))
    e[:,0] = y_pred.reshape(-1)
    e[:,1] = y_true
    f = pd.DataFrame(e)
    m1,m2,fact = 0,100,100
    x = np.array([f1_score(y_pred=f.loc[:,0]>thr/fact, y_true=f.loc[:,1]) for thr in range(m1,m2)])
    f1_, thresh = max(x),list(range(m1,m2))[x.argmax()]/fact
    return f.corr()[0][1], f1_, thresh

def f1(model,x,y):
    y_ = model.predict(x,batch_size=batch_size)
    return r_f1_thresh(y_,y) 

def get_embedding_layers(dtype, input_length, w2v_length, with_weight=True):
    def __get_embedding_layers(dtype, input_length, w2v_length, with_weight=True):

        if dtype == 'word':
            embedding_length = len(word2index)
        elif dtype == 'char':
            embedding_length = len(char2index)

        if with_weight:
            if ebed_type == "gensim":
                if dtype == 'word':
                    embedding = word_embedding_model.wv.get_keras_embedding()
                else:
                    embedding = char_embedding_model.wv.get_keras_embedding()

            elif ebed_type == "fastskip" or ebed_type == "fastcbow":
                if dtype == 'word':
                    embedding = Embedding(embedding_length, w2v_length, input_length=input_length, weights=[word_embedding_matrix], trainable=False)
                else:
                    embedding = Embedding(embedding_length, w2v_length, input_length=input_length, weights=[char_embedding_matrix], trainable=False)
        else:
            embedding = Embedding(embedding_length, w2v_length, input_length=input_length)

        return embedding

    if dtype == "both":
        embedding = []
        for dtype,input_length in zip(['word', 'char'],input_length):
            embedding.append(__get_embedding_layers(dtype, input_length, w2v_length, with_weight))
        return embedding
    else:
        return __get_embedding_layers(dtype, input_length, w2v_length, with_weight)

def double_train(train_x, train_y):
    train_x_mirror = [train_x[i] for i in [1,0,3,2]] if len(train_x)==4 else train_x[::-1]
    double_x = [np.concatenate((x1,x2)) for x1,x2 in zip(train_x, train_x_mirror)] 
    double_y = np.concatenate((train_y, train_y))
    return double_x, double_y

def get_model(cfg,model_weights=None):
    print("=======   CONFIG: ", cfg)

    model_type,dtype,input_length,w2v_length,n_hidden,n_epoch,patience = cfg
    embedding = get_embedding_layers(dtype, input_length, w2v_length, with_weight=True)

    if model_type == "esim":
        model = esim(pretrained_embedding=embedding, 
            maxlen=input_length, 
            lstm_dim=300, 
            dense_dim=300, 
            dense_dropout=0.5)
    elif model_type == "decom":
        model = decomposable_attention(pretrained_embedding=embedding, 
            projection_dim=300, projection_hidden=0, projection_dropout=0.2,
            compare_dim=500, compare_dropout=0.2,
            dense_dim=300, dense_dropout=0.2,
            lr=1e-3, activation='elu', maxlen=input_length)
    elif model_type == "siamese":
        model = siamese(pretrained_embedding=embedding, input_length=input_length, w2v_length=w2v_length, n_hidden=n_hidden)
    elif model_type == "dssm":
        model = DSSM(pretrained_embedding=embedding,input_length=input_length, lstmsize=90)

    if model_weights is not None:
        model.load_weights(model_weights)

    # keras.utils.plot_model(model, to_file=model_dir+model_type+"_"+dtype+'.png', show_shapes=True, show_layer_names=True, rankdir='TB')
    return model

def train_model(model, cfg):
    model_type,dtype,input_length,w2v_length,n_hidden,n_epoch,patience = cfg

    data = load_data(dtype, input_length, w2v_length)
    if test_size>0:
        train_x, train_y, test_x, test_y = split_data(data)
        # train_x, train_y = double_train(train_x, train_y) # 没效果
        # filepath=model_dir+model_type+"_"+dtype+"-{epoch:02d}-{val_loss:.4f}.h5"
        filepath=model_dir+model_type+"_"+dtype+".h5"
        checkpoint = ModelCheckpoint(filepath, monitor='val_loss', verbose=0, save_best_only=True,save_weights_only=True, mode='auto')
        earlystop = EarlyStopping(monitor='val_loss', min_delta=0, patience=patience, verbose=0, mode='auto')
        callbacks = [checkpoint, earlystop]
        def fit(n_epoch=n_epoch):
            history = model.fit(x=train_x, y=train_y,
                class_weight={0:1/np.mean(train_y),1:1/(1-np.mean(train_y))},
                validation_data=((test_x, test_y)),
                batch_size=batch_size, 
                callbacks=callbacks, 
                epochs=n_epoch,verbose=2)
            return history

        loss,metrics = 'binary_crossentropy',['binary_crossentropy',"accuracy"]
 
        reduce_lr = ReduceLROnPlateau(monitor='val_loss', verbose=0, factor=0.5,patience=2, min_lr=1e-6)
        callbacks.append(reduce_lr)
        pre_epoch = int(0.1*n_epoch)
        model.compile(optimizer=Adam(), loss=loss, metrics=metrics)
        if pre_epoch:fit(pre_epoch)
        model.compile(optimizer=SGD(lr=5e-2, momentum=0.8, nesterov=False), loss=loss, metrics=metrics)
        fit(n_epoch - pre_epoch)
        # model.load_weights(filepath)

    else:   # 确定训练次数后，不需要earlystop
        train_x, train_y = data[:-1], data[-1]
        filepath=model_dir+model_type+"_"+dtype+"_all.h5"
        history = model.fit(x=train_x, y=train_y,
            class_weight={0:1/np.mean(train_y),1:1/(1-np.mean(train_y))},
            batch_size=batch_size, 
            epochs=n_epoch,verbose=2)
        model.save_weights(filepath)
    if False and not online:
        import matplotlib.pyplot as plt
        plt.plot(history.history["val_loss"])
        plt.show()
    return model


def train_model_cv(cfg,cv=10):
    model_type,dtype,input_length,w2v_length,n_hidden,n_epoch,patience = cfg
    model_file = model_dir+model_type+"_"+dtype+".h5"

    data = load_data(dtype, input_length, w2v_length)

    data, data_test = split_data(data, mode="orig")  # 划分训练和测试

    best_epochs = []
    val_logs = []
    kf = KFold(n_splits=cv)     # 划分训练和验证
    for submodel_index, (train, val) in enumerate(kf.split(data[-1])):
        train_x, train_y, val_x, val_y = split_data_index(data, train_index = train, test_index = val)

        K.clear_session()
        model = get_model(cfg)
        # filepath=model_dir+model_type+"_"+dtype+"-{epoch:02d}-{val_loss:.4f}.h5"
        filepath=model_dir+model_type+"_"+dtype+"_cv%02d"%(submodel_index)+".h5"
        checkpoint = ModelCheckpoint(filepath, monitor='val_loss', verbose=1, save_best_only=True,save_weights_only=True, mode='auto')
        earlystop = EarlyStopping(monitor='val_loss', min_delta=0, patience=patience, verbose=1, mode='auto')
        reduce_lr = ReduceLROnPlateau(monitor='val_loss', verbose=1, factor=0.5,patience=plateau_patience, min_lr=1e-5)
        # train_x, train_y = double_train(train_x, train_y) # 没效果
        
        history = model.fit(x=train_x, y=train_y,
            class_weight={0:1/np.mean(train_y),1:1/(1-np.mean(train_y))},
            validation_data=((val_x, val_y)),
            batch_size=batch_size, 
            callbacks=[checkpoint,reduce_lr,earlystop], 
            epochs=MAX_EPOCH,verbose=2)
        model.load_weights(filepath)

        cv_best_epoch = np.array(history.history["val_loss"]).argmin()
        best_epochs.append(cv_best_epoch + 1)
        val_logs.append(f1(model, val_x, val_y))

        if model_type!="siamese":
            os.remove(filepath) # 删除子模型文件

    print(best_epochs)
    val_logs = np.array(val_logs)
    best_epoch = int(np.mean(best_epochs))
    val_log = val_logs.mean(axis=0)

    # 用最好的epoch重新训练
    train_x, train_y = data[:-1], data[-1]

    K.clear_session()
    model = get_model(cfg)
    # filepath=model_dir+model_type+"_"+dtype+"_all.h5"
    filepath=model_dir+model_type+"_"+dtype+".h5"
    model.fit(x=train_x, y=train_y,
        class_weight={0:1/np.mean(train_y),1:1/(1-np.mean(train_y))},
        batch_size=batch_size, 
        epochs=best_epoch,verbose=2)
    model.save_weights(filepath)

    # 测试模型
    test_x, test_y = data_test[:-1], data_test[-1]
    test_log = f1(model, test_x, test_y)
    print(best_epoch, val_log, test_log)
    return best_epoch, val_log, test_log


class Ensemble(object):
    def __init__(self, cfgs, weights):
        K.clear_session()
        self.cfgs = cfgs
        self.models = [get_model(cfg, weight) for cfg,weight in zip(cfgs,weights)]

    def predict(self,xs,batch_size):
        y = np.stack([model.predict(x,batch_size=batch_size).reshape(-1) for x,model in zip(xs,self.models)]).mean(axis=0).reshape(-1)
        return y

    def test(self):
        train_xs, test_xs = [],[]
        for index,(model_type, dtype,input_length,w2v_length,n_hidden,n_epoch,patience) in enumerate(self.cfgs):
            data = load_data(dtype, input_length, w2v_length)
            train_x, train_y, test_x, test_y = split_data(data)
            train_xs.append(train_x)
            test_xs.append(test_x)
        train_f1 = f1(self, train_xs, y=train_y)
        test_f1 = f1(self, test_xs, y=test_y)
        print(train_f1)
        print(test_f1)

def train_all_models():
    for cfg in cfgs:
        K.clear_session()
        model = get_model(cfg,None)
        train_model(model, cfg)

# test_size = 0 # train_all_models_with_all_data
train_all_models()

def train_all_models_cv():
    all_tables = []
    for cfg in cfgs[4:5]:
        table = train_model_cv(cfg)
        all_tables.append(table)
    print(all_tables)
# train_all_models_cv()

# ensemble = Ensemble([cfgs[0]]*10,[model_dir + "%s_%s_cv%02d.h5"%(cfgs[0][0],cfgs[0][1],i) for i in range(10)])
# ensemble.test()

# ensemble = Ensemble([cfgs[1]]*10,[model_dir + "%s_%s_cv%02d.h5"%(cfgs[1][0],cfgs[1][1],i) for i in range(10)])
# ensemble.test()

def evaluate_models():
    train_y_preds, test_y_preds = [], []
    for cfg,weight in zip(cfgs,weights):
        K.clear_session()
        model_type,dtype,input_length,w2v_length,n_hidden,n_epoch,patience = cfg        
        data = load_data(dtype, input_length, w2v_length)
        train_x, train_y, test_x, test_y = split_data(data)
        model = get_model(cfg,weight)
        train_y_preds.append(model.predict(train_x, batch_size=batch_size).reshape(-1))
        test_y_preds.append(model.predict(test_x, batch_size=batch_size).reshape(-1))

        # 确认对称性
        # train_x = [train_x[i] for i in [1,0,3,2]] if len(train_x)==4 else train_x[::-1]
        # test_x  = [test_x[i] for i in [1,0,3,2]]  if len(test_x)==4  else test_x[::-1]
        # train_y_preds.append(model.predict(train_x, batch_size=batch_size).reshape(-1))
        # test_y_preds.append(model.predict(test_x, batch_size=batch_size).reshape(-1))

    train_y_preds,test_y_preds = np.array(train_y_preds),np.array(test_y_preds)
    pd.to_pickle([train_y_preds,train_y,test_y_preds,test_y],model_dir + "y_pred.pkl")
    for train_y_pred, test_y_pred  in zip(train_y_preds,test_y_preds):
        print("train phrase:",r_f1_thresh(train_y_pred, train_y))
        print("test  phrase:",r_f1_thresh(test_y_pred, test_y))

def update_one_model(index = 2):
    train_y_preds,train_y,test_y_preds,test_y = pd.read_pickle(model_dir + "y_pred.pkl")
    cfg = cfgs[index]
    weight = weights[index]
    model_type,dtype,input_length,w2v_length,n_hidden,n_epoch,patience = cfg
    data = load_data(dtype, input_length, w2v_length)
    train_x, train_y, test_x, test_y = split_data(data)
    model = get_model(cfg,weight)
    train_y_preds[index] = model.predict(train_x, batch_size=batch_size).reshape(-1)
    test_y_preds[index] = model.predict(test_x, batch_size=batch_size).reshape(-1)
    pd.to_pickle([train_y_preds,train_y,test_y_preds,test_y],model_dir + "y_pred.pkl")

def find_out_combine_mean():
    train_y_preds,train_y,test_y_preds,test_y = pd.read_pickle(model_dir + "y_pred.pkl")
    with open(model_dir + "combine.txt","w",encoding="utf8") as log:
        for cb in combines:
            train_y_pred, test_y_pred = train_y_preds[cb].mean(axis=0),test_y_preds[cb].mean(axis=0)
            train_log = r_f1_thresh(train_y_pred, train_y)
            test_log = r_f1_thresh(test_y_pred, test_y)
            print(cb)
            print("train phrase:",train_log)
            print("test  phrase:",test_log)
            log.write("\t".join([str(cb),"\t".join(map(str,train_log)),"\t".join(map(str,test_log))])+"\n")

def find_out_combine_vote():
    train_y_preds,train_y,test_y_preds,test_y = pd.read_pickle(model_dir + "y_pred.pkl")

    if not online:
        train_thresh = [0.0900, 0.1100, 0.1200, 0.1500, 0.1600 ]
        test_thresh = [0.0900, 0.0900, 0.1000, 0.1400, 0.1600 ]
    else:
        train_thresh = [0.2700, 0.2600 ,0.3000 ,0.3300 ,0.2900 ]
        test_thresh = [0.2500 ,0.2600 ,0.2400 ,0.3100 ,0.2700  ]
    for cb in combines:
        train_y_pred, test_y_pred = train_y_preds[cb],test_y_preds[cb]
        for y_i, cb_i in enumerate(cb):
            train_y_pred[y_i], test_y_pred[y_i] = train_y_pred[y_i]>train_thresh[cb_i],test_y_pred[y_i]>test_thresh[cb_i]
        
        train_y_pred, test_y_pred = train_y_pred.sum(axis=0), test_y_pred.sum(axis=0)
        train_f1,test_f1 = np.zeros(len(cb)), np.zeros(len(cb))
        for vote in range(len(cb)):
            train_f1[vote] = f1_score(train_y_pred > vote, train_y)
            test_f1[vote]  = f1_score(test_y_pred > vote, test_y)

        print(cb)
        print("train phrase:",train_f1.max(), train_f1.argmax())
        print("test  phrase:",test_f1.max(),  test_f1.argmax())

def find_out_combine_lr():
    train_y_preds,train_y,test_y_preds,test_y = pd.read_pickle(model_dir + "y_pred.pkl")
    lr = LogisticRegressionCV()
    lr.fit(train_y_preds.T,train_y)
    test_pred = lr.predict(test_y_preds.T)
    print("test phrase:", f1_score(y_pred=test_pred, y_true=test_y))

# evaluate_models()
# find_out_combine_mean()

# find_out_combine_vote()
# find_out_combine_lr()

def result(df1):
    # to evaluate for the result
    if not online:
        best_threshold = 0.1400 
    else:
        best_threshold = 0.2900

    K.clear_session()

    best_models = [0]
    # best_models = [0,2,4,5]
    best_cfgs = [cfgs[i] for i in best_models]
    best_wgts = [weights[i] for i in best_models]

    train_xs = []
    for cfg in best_cfgs:
        model_type,dtype,input_length,w2v_length,n_hidden,n_epoch,patience = cfg
        X = input_data(df1["sent1"],df1["sent2"], dtype =dtype, input_length=input_length)
        train_xs.append(X)
        
    enmodel = Ensemble(best_cfgs,best_wgts)
    result = enmodel.predict(train_xs,batch_size=batch_size) > best_threshold
    df_output = pd.concat([df1["id"],pd.Series(result,name="label",dtype=np.int32)],axis=1)
    if not online:
        print(df_output)
    else:
        topai(1,df_output)

if not online:
    df1 = pd.read_csv(model_dir+"atec_nlp_sim_train.csv",sep="\t", header=None, encoding="utf8")
    df1.columns=["id","sent1","sent2","label"]

# result(df1)