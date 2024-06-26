import argparse
import os
import sys
import shutil
import random
from matplotlib import pyplot as plt
import numpy as np
import time
import copy
import math
import pickle

import torch
import torch.nn.functional as F
import torch.nn as nn
from torch.autograd import Variable
from transformers import GPT2TokenizerFast
from torch.utils.data import Dataset
from torch.utils.data import DataLoader

train_losses = []
vali_losses = []


#change 
class TextDataset(Dataset):
    def __init__(self, data):
        self.data = data

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        return self.data[idx]


def read_corpus(filename,tokenizer):
    seq = []
    with open(filename,'rt') as f:
        for line in f:
            line = line.replace('\n','')
            tokens = tokenizer(line)
            for t in tokens['input_ids']:
                seq.append(t)
    return(seq)

class Embedder(nn.Module):
    def __init__(self, vocab_size, d_model):
        super().__init__()
        self.d_model = d_model
        self.embed = nn.Embedding(vocab_size, d_model)
    def forward(self, x):
        return self.embed(x.int())

class PositionalEncoder(nn.Module):
    def __init__(self, d_model, max_seq_len = 4096, dropout = 0.1):
        super().__init__()
        self.d_model = d_model
        self.dropout = nn.Dropout(dropout)
        # create constant 'pe' matrix with values dependant on 
        # pos and i
        pe = torch.zeros(max_seq_len, d_model)
        for pos in range(max_seq_len):
            for i in range(0, d_model, 2):
                pe[pos, i] = \
                math.sin(pos / (10000 ** ((2 * i)/d_model)))
                pe[pos, i + 1] = \
                math.cos(pos / (10000 ** ((2 * (i + 1))/d_model)))
        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)
    
    def forward(self, x):
        # make embeddings relatively larger
        x = x * math.sqrt(self.d_model)
        #add constant to embedding
        seq_len = x.size(1)
        pe = Variable(self.pe[:,:seq_len], requires_grad=False)
        if x.is_cuda:
            pe.cuda()
        x = x + pe
        return self.dropout(x)

class Norm(nn.Module):
    def __init__(self, d_model, eps = 1e-6):
        super().__init__()
    
        self.size = d_model
        
        # create two learnable parameters to calibrate normalisation
        self.alpha = nn.Parameter(torch.ones(self.size))
        self.bias = nn.Parameter(torch.zeros(self.size))
        
        self.eps = eps
    
    def forward(self, x):
        norm = self.alpha * (x - x.mean(dim=-1, keepdim=True)) \
        / (x.std(dim=-1, keepdim=True) + self.eps) + self.bias
        return norm

def attention(q, k, v, d_k, mask=None, dropout=None):
    
    scores = torch.matmul(q, k.transpose(-2, -1)) /  math.sqrt(d_k)
    
    if mask is not None:
        mask = mask.unsqueeze(1)
        scores = scores.masked_fill(mask == 0, -1e9)
    
    scores = F.softmax(scores, dim=-1)
    
    if dropout is not None:
        scores = dropout(scores)
        
    output = torch.matmul(scores, v)
    return output

class MultiHeadAttention(nn.Module):
    def __init__(self, heads, d_model, dropout = 0.1):
        super().__init__()
        
        self.d_model = d_model
        self.d_k = d_model // heads
        self.h = heads
        
        self.q_linear = nn.Linear(d_model, d_model)
        self.v_linear = nn.Linear(d_model, d_model)
        self.k_linear = nn.Linear(d_model, d_model)
        
        self.dropout = nn.Dropout(dropout)
        self.out = nn.Linear(d_model, d_model)
    
    def forward(self, q, k, v, mask=None):
        
        bs = q.size(0)
        
        # perform linear operation and split into N heads
        k = self.k_linear(k).view(bs, -1, self.h, self.d_k)
        q = self.q_linear(q).view(bs, -1, self.h, self.d_k)
        v = self.v_linear(v).view(bs, -1, self.h, self.d_k)
        
        # transpose to get dimensions bs * N * sl * d_model
        k = k.transpose(1,2)
        q = q.transpose(1,2)
        v = v.transpose(1,2)
        

        # calculate attention using function we will define next
        scores = attention(q, k, v, self.d_k, mask, self.dropout)
        # concatenate heads and put through final linear layer
        concat = scores.transpose(1,2).contiguous()\
        .view(bs, -1, self.d_model)
        output = self.out(concat)
    
        return output

class FeedForward(nn.Module):
    def __init__(self, d_model, d_ff=2048, dropout = 0.1):
        super().__init__() 
    
        # We set d_ff as a default to 2048
        self.linear_1 = nn.Linear(d_model, d_ff)
        self.dropout = nn.Dropout(dropout)
        self.linear_2 = nn.Linear(d_ff, d_model)
    
    def forward(self, x):
        x = self.dropout(F.relu(self.linear_1(x)))
        x = self.linear_2(x)
        return x
    
def get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])

class CosineWithRestarts(torch.optim.lr_scheduler._LRScheduler):
    """
    Cosine annealing with restarts.

    Parameters
    ----------
    optimizer : torch.optim.Optimizer

    T_max : int
        The maximum number of iterations within the first cycle.

    eta_min : float, optional (default: 0)
        The minimum learning rate.

    last_epoch : int, optional (default: -1)
        The index of the last epoch.

    """

    def __init__(self,
                 optimizer: torch.optim.Optimizer,
                 T_max: int,
                 eta_min: float = 0.,
                 last_epoch: int = -1,
                 factor: float = 1.) -> None:
        # pylint: disable=invalid-name
        self.T_max = T_max
        self.eta_min = eta_min
        self.factor = factor
        self._last_restart: int = 0
        self._cycle_counter: int = 0
        self._cycle_factor: float = 1.
        self._updated_cycle_len: int = T_max
        self._initialized: bool = False
        super(CosineWithRestarts, self).__init__(optimizer, last_epoch)

    def get_lr(self):
        """Get updated learning rate."""
        # HACK: We need to check if this is the first time get_lr() was called, since
        # we want to start with step = 0, but _LRScheduler calls get_lr with
        # last_epoch + 1 when initialized.
        if not self._initialized:
            self._initialized = True
            return self.base_lrs

        step = self.last_epoch + 1
        self._cycle_counter = step - self._last_restart

        lrs = [
            (
                self.eta_min + ((lr - self.eta_min) / 2) *
                (
                    np.cos(
                        np.pi *
                        ((self._cycle_counter) % self._updated_cycle_len) /
                        self._updated_cycle_len
                    ) + 1
                )
            ) for lr in self.base_lrs
        ]

        if self._cycle_counter % self._updated_cycle_len == 0:
            # Adjust the cycle length.
            self._cycle_factor *= self.factor
            self._cycle_counter = 0
            self._updated_cycle_len = int(self._cycle_factor * self.T_max)
            self._last_restart = step

        return lrs
    
class EncoderLayer(nn.Module):
    def __init__(self, d_model, heads, dropout=0.1):
        super().__init__()
        self.norm_1 = Norm(d_model)
        self.norm_2 = Norm(d_model)
        self.attn = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.ff = FeedForward(d_model, dropout=dropout)
        self.dropout_1 = nn.Dropout(dropout)
        self.dropout_2 = nn.Dropout(dropout)
        
    def forward(self, x, mask):
        x2 = self.norm_1(x)
        x = x + self.dropout_1(self.attn(x2,x2,x2,mask))
        x2 = self.norm_2(x)
        x = x + self.dropout_2(self.ff(x2))
        return x
    
# build a decoder layer with two multi-head attention layers and
# one feed-forward layer
class DecoderLayer(nn.Module):
    def __init__(self, d_model, heads, dropout=0.1):
        super().__init__()
        self.norm_1 = Norm(d_model)
        self.norm_2 = Norm(d_model)
        self.norm_3 = Norm(d_model)
        
        self.dropout_1 = nn.Dropout(dropout)
        self.dropout_2 = nn.Dropout(dropout)
        self.dropout_3 = nn.Dropout(dropout)
        
        self.attn_1 = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.attn_2 = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.ff = FeedForward(d_model, dropout=dropout)

    def forward(self, x, e_outputs, src_mask, trg_mask):
        x2 = self.norm_1(x)
        x = x + self.dropout_1(self.attn_1(x2, x2, x2, trg_mask))
        x2 = self.norm_2(x)
        x = x + self.dropout_2(self.attn_2(x2, e_outputs, e_outputs, \
        src_mask))
        x2 = self.norm_3(x)
        x = x + self.dropout_3(self.ff(x2))
        return x    
    
class Encoder(nn.Module):
    def __init__(self, vocab_size, d_model, N, heads, dropout):
        super().__init__()
        self.N = N
        self.embed = Embedder(vocab_size, d_model)
        self.pe = PositionalEncoder(d_model, dropout=dropout)
        self.layers = get_clones(EncoderLayer(d_model, heads, dropout), N)
        self.norm = Norm(d_model)
    def forward(self, src, mask):
        x = self.embed(src)
        x = self.pe(x)
        for i in range(self.N):
            x = self.layers[i](x, mask)
        return self.norm(x)
    
class Decoder(nn.Module):
    def __init__(self, vocab_size, d_model, N, heads, dropout):
        super().__init__()
        self.N = N
        self.embed = Embedder(vocab_size, d_model)
        self.pe = PositionalEncoder(d_model, dropout=dropout)
        self.layers = get_clones(DecoderLayer(d_model, heads, dropout), N)
        self.norm = Norm(d_model)
    def forward(self, trg, e_outputs, src_mask, trg_mask):
        x = self.embed(trg)
        x = self.pe(x)
        for i in range(self.N):
            x = self.layers[i](x, e_outputs, src_mask, trg_mask)
        return self.norm(x)

# Decoder only, no cross attention
# change 
class DecoderOnlyLayer(nn.Module):
    def __init__(self, d_model, heads, dropout=0.1):
        super().__init__()
        self.norm_1 = Norm(d_model)
        self.norm_2 = Norm(d_model)
        self.norm_3 = Norm(d_model)
        
        self.dropout_1 = nn.Dropout(dropout)
        self.dropout_2 = nn.Dropout(dropout)
        self.dropout_3 = nn.Dropout(dropout)
        
        self.attn_1 = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.attn_2 = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.ff = FeedForward(d_model, dropout=dropout)

    def forward(self, x, trg_mask):
        x2 = self.norm_1(x)
        x = x + self.dropout_1(self.attn_1(x2, x2, x2, trg_mask))
        x2 = self.norm_2(x)
        x = x + self.dropout_2(self.attn_2(x2, x2, x2, trg_mask))
        x2 = self.norm_3(x)
        x = x + self.dropout_3(self.ff(x2))
        return x 


class DecoderOnly(nn.Module):
    def __init__(self, vocab_size, d_model, N, heads, dropout):
        super().__init__()
        self.N = N
        self.embed = Embedder(vocab_size, d_model)
        self.pe = PositionalEncoder(d_model, dropout=dropout)
        self.layers = get_clones(DecoderOnlyLayer(d_model, heads, dropout), N)
        self.norm = Norm(d_model)
    def forward(self, trg, trg_mask):
        x = self.embed(trg)
        x = self.pe(x)
        for i in range(self.N):
            x = self.layers[i](x, trg_mask)
        return self.norm(x)

#change 
class Transformer(nn.Module):
    def __init__(self, trg_vocab, d_model, N, heads, dropout):
        super().__init__()
        #self.encoder = Encoder(src_vocab, d_model, N, heads, dropout)
        self.decoder = DecoderOnly(trg_vocab, d_model, N, heads, dropout)
        self.out = nn.Linear(d_model, trg_vocab)
    def forward(self, trg, trg_mask):
        #e_outputs = self.encoder(src, src_mask)
        #print("DECODER")
        #d_output = self.decoder(trg, e_outputs, src_mask, trg_mask)
        d_output = self.decoder(trg, trg_mask)
        output = self.out(d_output)
        return output

def get_model(opt, src_vocab, trg_vocab):
    
    assert opt.d_model % opt.heads == 0
    assert opt.dropout < 1

    #model = Transformer(src_vocab, trg_vocab, opt.d_model, opt.n_layers, opt.heads, opt.dropout)
    model = Transformer(trg_vocab, opt.d_model, opt.n_layers, opt.heads, opt.dropout)
    model.to(opt.device)
       
    if opt.loadname is not None:
        print("loading pretrained weights...")
        model.load_state_dict(torch.load(opt.loadname))
    else:
        for p in model.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p) 
    
    return model
    
def train_model(model, opt, train_loader,valid_loader):
    # write code to:
    #  1. create a nopeak mask
    #  2. feed training data to the model in batches
    #  3. send the indices of training tokens to the GPU
    #  4. linearize the predictions and compute the loss against ground truth
    #     (you can use F.cross_entropy or write your own code)
    #  5. calculate and apply the gradients with loss.backward() and optimizer.step()
    #  6. report intermediate trainining perplexity
    #  7. generate a test perplexity once per training epoch by calling test_model()
    #  8. save model weights to file specified in opt.savename
    #  SEE trainer.py for examples of each of the above
    print("training model...")
    model.train()

    #train loop
    for epoch in range(opt.epochs):
        print("epoch %d" % (epoch))
        total_train_loss = 0
        total_train_tokens = 0
        #print(len(train_loader))
        inputmask = torch.triu(torch.ones((1, 511, 511), device=opt.device), diagonal=1).bool()
        inputmask = ~inputmask
        for batch in train_loader:
            # Your training logic here
            inputs, targets = batch[:,:-1], batch[:,1:]  # Input is the current token, target is the next token
            inputs, targets = inputs.to(opt.device), targets.to(opt.device)  # Ensure data is on the correct device

            # Forward pass
            outputs = model(inputs, inputmask)
            loss = F.cross_entropy(outputs.view(-1, outputs.size(-1)), targets.view(-1))

            # Backward and optimize
            opt.optimizer.zero_grad()
            loss.backward()
            opt.optimizer.step()
            
            if opt.SGDR == True:
                opt.sched.step()

            total_train_loss += loss.item() * targets.numel()
            total_train_tokens += targets.numel()
            #total_train_loss += loss.item() * inputs.size(0)
            #total_train_tokens += inputs.size(0)

        train_perplexity = torch.exp(torch.tensor(total_train_loss / total_train_tokens))
        print(f"Train perplexity: {train_perplexity}")
        train_losses.append(train_perplexity)
            
        model.eval()  # Set the model to evaluation mode
        total_val_loss = 0
        total_val_tokens = 0

        with torch.no_grad():  # Disable gradient computation
            for batch in valid_loader:
                inputs, targets = batch[:,:-1], batch[:,1:]
                inputs, targets = inputs.to(opt.device), targets.to(opt.device)

                # Forward pass
                # inputmask = torch.triu(torch.ones((1, inputs.size(1), inputs.size(1)), device=opt.device), diagonal=1).bool()
                outputs = model(inputs, inputmask)
                loss = F.cross_entropy(outputs.view(-1, outputs.size(-1)), targets.view(-1))

                total_val_loss += loss.item() * targets.numel()
                total_val_tokens += targets.numel()
                #total_val_loss += loss.item() * inputs.size(0)
                #total_val_tokens += inputs.size(0)
                
            val_perplexity = torch.exp(torch.tensor(total_val_loss / total_val_tokens))
            print(f"Validation perplexity: {val_perplexity}")
            vali_losses.append(val_perplexity)
    
    
def test_model(model, opt, epoch, test_loader):
    print("testing model...")
    model.eval()
    total_test_loss = 0
    total_test_tokens = 0  
    with torch.no_grad():  # Disable gradient computation
        inputmask = torch.triu(torch.ones((1, 511, 511), device=opt.device), diagonal=1).bool()
        inputmask = ~inputmask
        for batch in test_loader:
            inputs, targets = batch[:,:-1], batch[:,1:]
            inputs, targets = inputs.to(opt.device), targets.to(opt.device)

            # Forward pass
            #inputmask = torch.triu(torch.ones((1, inputs.size(1), inputs.size(1)), device=opt.device), diagonal=1).bool()
            outputs = model(inputs, inputmask)
            loss = F.cross_entropy(outputs.view(-1, outputs.size(-1)), targets.view(-1))
            
            total_test_loss += loss.item() * targets.numel()
            total_test_tokens += targets.numel()
            #total_test_loss += loss.item() * inputs.size(0)
            #total_test_tokens += inputs.size(0)
            
        test_perplexity = torch.exp(torch.tensor(total_test_loss / total_test_tokens))
        print(f"Test perplexity: {test_perplexity}")
    model.train()
    


def main():
    
    random.seed(10)
    
    parser = argparse.ArgumentParser()
    parser.add_argument('-no_cuda', action='store_true')
    parser.add_argument('-SGDR', action='store_true')
    parser.add_argument('-epochs', type=int, default=20)
    parser.add_argument('-d_model', type=int, default=512)
    parser.add_argument('-n_layers', type=int, default=6)
    parser.add_argument('-heads', type=int, default=8)
    parser.add_argument('-dropout', type=int, default=0.1)
    parser.add_argument('-batchsize', type=int, default=3)
    parser.add_argument('-printevery', type=int, default=100)
    parser.add_argument('-lr', type=int, default=0.00001)
    parser.add_argument('-seqlen', type=int, default=512)
    parser.add_argument('-threshold', type=int, default=3)
    parser.add_argument('-savename', type=str)    
    parser.add_argument('-loadname', type=str)    
    parser.add_argument('-tied', type=int, default=1)
    parser.add_argument('-dir_name', type=str,default='model')
    parser.add_argument('-norm', type=float, default=2.0)
                
    opt = parser.parse_args()
    opt.verbose = False    
    
    opt.device = 0 if opt.no_cuda is False else -1
    if opt.device == 0:
       assert torch.cuda.is_available()
    opt.device = torch.device("cuda:0")
    #opt.device = torch.device("cpu")
    time_name = time.strftime("%y%m%d_%H%M%S")
    opt.time_name = time_name
    dir_name = "saved/%s" % (opt.dir_name)
    if not os.path.exists(dir_name):
        os.makedirs(dir_name)
    source_name = sys.argv[0]
    dir_name = dir_name + "//"
    opt.dir_name = dir_name
    shutil.copy(source_name,dir_name + source_name)
    opt.log_file = dir_name + "log_file.txt"
    
    print(str(opt))
    tokenizer = GPT2TokenizerFast.from_pretrained("gpt2")
    opt.train = read_corpus('wiki2.train.txt',tokenizer)
    opt.valid = read_corpus('wiki2.valid.txt',tokenizer)
    opt.test = read_corpus('wiki2.test.txt',tokenizer)
    
    
    
    #change 
    def create_fixed_length_sequences(data, sequence_length):
        # Split the data into chunks of `sequence_length`, discarding the remainder
        num_full_batches = len(data) // sequence_length
        # Slice the data to ensure it's a multiple of `sequence_length`
        truncated_length = num_full_batches * sequence_length
        sequences = data[:truncated_length].view(-1, sequence_length)
        return sequences
    
    print(len(opt.train))
    train_dataset = torch.tensor(opt.train)
    train_dataset = create_fixed_length_sequences(train_dataset,opt.seqlen)
    train_dataset = TextDataset(train_dataset)
    print(len(train_dataset))
    
    valid_dataset = torch.tensor(opt.valid)
    valid_dataset = create_fixed_length_sequences(valid_dataset,opt.seqlen)
    valid_dataset = TextDataset(valid_dataset)
    
    test_dataset = torch.tensor(opt.test)
    test_dataset = create_fixed_length_sequences(test_dataset,opt.seqlen)
    test_dataset = TextDataset(test_dataset)
    

    batch_size = opt.batchsize

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, drop_last=True)
    valid_loader = DataLoader(valid_dataset, batch_size=batch_size, shuffle=True, drop_last=True)
    test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=True, drop_last=True)
    
    obs = len(opt.train)
    opt.vocab_size = 50257
    temp = []
    for i in range(opt.vocab_size):
        temp.append(i)
    opt.indices = torch.tensor(temp)
    opt.indices = opt.indices.cuda()
    
    model = get_model(opt,opt.vocab_size,opt.vocab_size)
        
    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])        
    text = 'total params: %d' % (params)
    print(text)

    opt.optimizer = torch.optim.Adam(model.parameters(), lr=opt.lr, betas=(0.9, 0.98), eps=1e-9)
    if opt.SGDR == True:
        opt.sched = CosineWithRestarts(opt.optimizer, T_max=opt.train_len)

    if opt.savename is not None:
        try:
            os.mkdir(opt.savename)
            torch.save(model.state_dict(), 'model_params.pth')
        except:
            nothing = 1
    opt.src_pad = 0
    opt.trg_pad = 0
    
    #change     
 
    train_model(model,opt,train_loader,valid_loader)
    test_model(model,opt,-1,test_loader)
    # torch.save(model, 'model.pth')
    
    
    plt.figure(figsize=(20, 15))
    plt.plot(train_losses, label='Training Loss')
    plt.plot(vali_losses, label='Validation Loss')
    plt.title('Training and Validation Losses')
    plt.xlabel('Epochs')
    plt.ylabel('Loss')
    plt.legend()
    plt.show()
        
        
        
        
        
if __name__ == "__main__":
    main()        
import argparse
import os
import sys
import shutil
import random
from matplotlib import pyplot as plt
import numpy as np
import time
import copy
import math
import pickle

import torch
import torch.nn.functional as F
import torch.nn as nn
from torch.autograd import Variable
from transformers import GPT2TokenizerFast
from torch.utils.data import Dataset
from torch.utils.data import DataLoader

train_losses = []
vali_losses = []


#change 
class TextDataset(Dataset):
    def __init__(self, data):
        self.data = data

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        return self.data[idx]


def read_corpus(filename,tokenizer):
    seq = []
    with open(filename,'rt') as f:
        for line in f:
            line = line.replace('\n','')
            tokens = tokenizer(line)
            for t in tokens['input_ids']:
                seq.append(t)
    return(seq)

class Embedder(nn.Module):
    def __init__(self, vocab_size, d_model):
        super().__init__()
        self.d_model = d_model
        self.embed = nn.Embedding(vocab_size, d_model)
    def forward(self, x):
        return self.embed(x.int())

class PositionalEncoder(nn.Module):
    def __init__(self, d_model, max_seq_len = 4096, dropout = 0.1):
        super().__init__()
        self.d_model = d_model
        self.dropout = nn.Dropout(dropout)
        # create constant 'pe' matrix with values dependant on 
        # pos and i
        pe = torch.zeros(max_seq_len, d_model)
        for pos in range(max_seq_len):
            for i in range(0, d_model, 2):
                pe[pos, i] = \
                math.sin(pos / (10000 ** ((2 * i)/d_model)))
                pe[pos, i + 1] = \
                math.cos(pos / (10000 ** ((2 * (i + 1))/d_model)))
        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)
    
    def forward(self, x):
        # make embeddings relatively larger
        x = x * math.sqrt(self.d_model)
        #add constant to embedding
        seq_len = x.size(1)
        pe = Variable(self.pe[:,:seq_len], requires_grad=False)
        if x.is_cuda:
            pe.cuda()
        x = x + pe
        return self.dropout(x)

class Norm(nn.Module):
    def __init__(self, d_model, eps = 1e-6):
        super().__init__()
    
        self.size = d_model
        
        # create two learnable parameters to calibrate normalisation
        self.alpha = nn.Parameter(torch.ones(self.size))
        self.bias = nn.Parameter(torch.zeros(self.size))
        
        self.eps = eps
    
    def forward(self, x):
        norm = self.alpha * (x - x.mean(dim=-1, keepdim=True)) \
        / (x.std(dim=-1, keepdim=True) + self.eps) + self.bias
        return norm

def attention(q, k, v, d_k, mask=None, dropout=None):
    
    scores = torch.matmul(q, k.transpose(-2, -1)) /  math.sqrt(d_k)
    
    if mask is not None:
        mask = mask.unsqueeze(1)
        scores = scores.masked_fill(mask == 0, -1e9)
    
    scores = F.softmax(scores, dim=-1)
    
    if dropout is not None:
        scores = dropout(scores)
        
    output = torch.matmul(scores, v)
    return output

class MultiHeadAttention(nn.Module):
    def __init__(self, heads, d_model, dropout = 0.1):
        super().__init__()
        
        self.d_model = d_model
        self.d_k = d_model // heads
        self.h = heads
        
        self.q_linear = nn.Linear(d_model, d_model)
        self.v_linear = nn.Linear(d_model, d_model)
        self.k_linear = nn.Linear(d_model, d_model)
        
        self.dropout = nn.Dropout(dropout)
        self.out = nn.Linear(d_model, d_model)
    
    def forward(self, q, k, v, mask=None):
        
        bs = q.size(0)
        
        # perform linear operation and split into N heads
        k = self.k_linear(k).view(bs, -1, self.h, self.d_k)
        q = self.q_linear(q).view(bs, -1, self.h, self.d_k)
        v = self.v_linear(v).view(bs, -1, self.h, self.d_k)
        
        # transpose to get dimensions bs * N * sl * d_model
        k = k.transpose(1,2)
        q = q.transpose(1,2)
        v = v.transpose(1,2)
        

        # calculate attention using function we will define next
        scores = attention(q, k, v, self.d_k, mask, self.dropout)
        # concatenate heads and put through final linear layer
        concat = scores.transpose(1,2).contiguous()\
        .view(bs, -1, self.d_model)
        output = self.out(concat)
    
        return output

class FeedForward(nn.Module):
    def __init__(self, d_model, d_ff=2048, dropout = 0.1):
        super().__init__() 
    
        # We set d_ff as a default to 2048
        self.linear_1 = nn.Linear(d_model, d_ff)
        self.dropout = nn.Dropout(dropout)
        self.linear_2 = nn.Linear(d_ff, d_model)
    
    def forward(self, x):
        x = self.dropout(F.relu(self.linear_1(x)))
        x = self.linear_2(x)
        return x
    
def get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])

class CosineWithRestarts(torch.optim.lr_scheduler._LRScheduler):
    """
    Cosine annealing with restarts.

    Parameters
    ----------
    optimizer : torch.optim.Optimizer

    T_max : int
        The maximum number of iterations within the first cycle.

    eta_min : float, optional (default: 0)
        The minimum learning rate.

    last_epoch : int, optional (default: -1)
        The index of the last epoch.

    """

    def __init__(self,
                 optimizer: torch.optim.Optimizer,
                 T_max: int,
                 eta_min: float = 0.,
                 last_epoch: int = -1,
                 factor: float = 1.) -> None:
        # pylint: disable=invalid-name
        self.T_max = T_max
        self.eta_min = eta_min
        self.factor = factor
        self._last_restart: int = 0
        self._cycle_counter: int = 0
        self._cycle_factor: float = 1.
        self._updated_cycle_len: int = T_max
        self._initialized: bool = False
        super(CosineWithRestarts, self).__init__(optimizer, last_epoch)

    def get_lr(self):
        """Get updated learning rate."""
        # HACK: We need to check if this is the first time get_lr() was called, since
        # we want to start with step = 0, but _LRScheduler calls get_lr with
        # last_epoch + 1 when initialized.
        if not self._initialized:
            self._initialized = True
            return self.base_lrs

        step = self.last_epoch + 1
        self._cycle_counter = step - self._last_restart

        lrs = [
            (
                self.eta_min + ((lr - self.eta_min) / 2) *
                (
                    np.cos(
                        np.pi *
                        ((self._cycle_counter) % self._updated_cycle_len) /
                        self._updated_cycle_len
                    ) + 1
                )
            ) for lr in self.base_lrs
        ]

        if self._cycle_counter % self._updated_cycle_len == 0:
            # Adjust the cycle length.
            self._cycle_factor *= self.factor
            self._cycle_counter = 0
            self._updated_cycle_len = int(self._cycle_factor * self.T_max)
            self._last_restart = step

        return lrs
    
class EncoderLayer(nn.Module):
    def __init__(self, d_model, heads, dropout=0.1):
        super().__init__()
        self.norm_1 = Norm(d_model)
        self.norm_2 = Norm(d_model)
        self.attn = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.ff = FeedForward(d_model, dropout=dropout)
        self.dropout_1 = nn.Dropout(dropout)
        self.dropout_2 = nn.Dropout(dropout)
        
    def forward(self, x, mask):
        x2 = self.norm_1(x)
        x = x + self.dropout_1(self.attn(x2,x2,x2,mask))
        x2 = self.norm_2(x)
        x = x + self.dropout_2(self.ff(x2))
        return x
    
# build a decoder layer with two multi-head attention layers and
# one feed-forward layer
class DecoderLayer(nn.Module):
    def __init__(self, d_model, heads, dropout=0.1):
        super().__init__()
        self.norm_1 = Norm(d_model)
        self.norm_2 = Norm(d_model)
        self.norm_3 = Norm(d_model)
        
        self.dropout_1 = nn.Dropout(dropout)
        self.dropout_2 = nn.Dropout(dropout)
        self.dropout_3 = nn.Dropout(dropout)
        
        self.attn_1 = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.attn_2 = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.ff = FeedForward(d_model, dropout=dropout)

    def forward(self, x, e_outputs, src_mask, trg_mask):
        x2 = self.norm_1(x)
        x = x + self.dropout_1(self.attn_1(x2, x2, x2, trg_mask))
        x2 = self.norm_2(x)
        x = x + self.dropout_2(self.attn_2(x2, e_outputs, e_outputs, \
        src_mask))
        x2 = self.norm_3(x)
        x = x + self.dropout_3(self.ff(x2))
        return x    
    
class Encoder(nn.Module):
    def __init__(self, vocab_size, d_model, N, heads, dropout):
        super().__init__()
        self.N = N
        self.embed = Embedder(vocab_size, d_model)
        self.pe = PositionalEncoder(d_model, dropout=dropout)
        self.layers = get_clones(EncoderLayer(d_model, heads, dropout), N)
        self.norm = Norm(d_model)
    def forward(self, src, mask):
        x = self.embed(src)
        x = self.pe(x)
        for i in range(self.N):
            x = self.layers[i](x, mask)
        return self.norm(x)
    
class Decoder(nn.Module):
    def __init__(self, vocab_size, d_model, N, heads, dropout):
        super().__init__()
        self.N = N
        self.embed = Embedder(vocab_size, d_model)
        self.pe = PositionalEncoder(d_model, dropout=dropout)
        self.layers = get_clones(DecoderLayer(d_model, heads, dropout), N)
        self.norm = Norm(d_model)
    def forward(self, trg, e_outputs, src_mask, trg_mask):
        x = self.embed(trg)
        x = self.pe(x)
        for i in range(self.N):
            x = self.layers[i](x, e_outputs, src_mask, trg_mask)
        return self.norm(x)

# Decoder only, no cross attention
# change 
class DecoderOnlyLayer(nn.Module):
    def __init__(self, d_model, heads, dropout=0.1):
        super().__init__()
        self.norm_1 = Norm(d_model)
        self.norm_2 = Norm(d_model)
        self.norm_3 = Norm(d_model)
        
        self.dropout_1 = nn.Dropout(dropout)
        self.dropout_2 = nn.Dropout(dropout)
        self.dropout_3 = nn.Dropout(dropout)
        
        self.attn_1 = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.attn_2 = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.ff = FeedForward(d_model, dropout=dropout)

    def forward(self, x, trg_mask):
        x2 = self.norm_1(x)
        x = x + self.dropout_1(self.attn_1(x2, x2, x2, trg_mask))
        x2 = self.norm_2(x)
        x = x + self.dropout_2(self.attn_2(x2, x2, x2, trg_mask))
        x2 = self.norm_3(x)
        x = x + self.dropout_3(self.ff(x2))
        return x 


class DecoderOnly(nn.Module):
    def __init__(self, vocab_size, d_model, N, heads, dropout):
        super().__init__()
        self.N = N
        self.embed = Embedder(vocab_size, d_model)
        self.pe = PositionalEncoder(d_model, dropout=dropout)
        self.layers = get_clones(DecoderOnlyLayer(d_model, heads, dropout), N)
        self.norm = Norm(d_model)
    def forward(self, trg, trg_mask):
        x = self.embed(trg)
        x = self.pe(x)
        for i in range(self.N):
            x = self.layers[i](x, trg_mask)
        return self.norm(x)

#change 
class Transformer(nn.Module):
    def __init__(self, trg_vocab, d_model, N, heads, dropout):
        super().__init__()
        #self.encoder = Encoder(src_vocab, d_model, N, heads, dropout)
        self.decoder = DecoderOnly(trg_vocab, d_model, N, heads, dropout)
        self.out = nn.Linear(d_model, trg_vocab)
    def forward(self, trg, trg_mask):
        #e_outputs = self.encoder(src, src_mask)
        #print("DECODER")
        #d_output = self.decoder(trg, e_outputs, src_mask, trg_mask)
        d_output = self.decoder(trg, trg_mask)
        output = self.out(d_output)
        return output

def get_model(opt, src_vocab, trg_vocab):
    
    assert opt.d_model % opt.heads == 0
    assert opt.dropout < 1

    #model = Transformer(src_vocab, trg_vocab, opt.d_model, opt.n_layers, opt.heads, opt.dropout)
    model = Transformer(trg_vocab, opt.d_model, opt.n_layers, opt.heads, opt.dropout)
    model.to(opt.device)
       
    if opt.loadname is not None:
        print("loading pretrained weights...")
        model.load_state_dict(torch.load(opt.loadname))
    else:
        for p in model.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p) 
    
    return model
    
def train_model(model, opt, train_loader,valid_loader):
    # write code to:
    #  1. create a nopeak mask
    #  2. feed training data to the model in batches
    #  3. send the indices of training tokens to the GPU
    #  4. linearize the predictions and compute the loss against ground truth
    #     (you can use F.cross_entropy or write your own code)
    #  5. calculate and apply the gradients with loss.backward() and optimizer.step()
    #  6. report intermediate trainining perplexity
    #  7. generate a test perplexity once per training epoch by calling test_model()
    #  8. save model weights to file specified in opt.savename
    #  SEE trainer.py for examples of each of the above
    print("training model...")
    model.train()

    #train loop
    for epoch in range(opt.epochs):
        print("epoch %d" % (epoch))
        total_train_loss = 0
        total_train_tokens = 0
        #print(len(train_loader))
        inputmask = torch.triu(torch.ones((1, 511, 511), device=opt.device), diagonal=1).bool()
        inputmask = ~inputmask
        for batch in train_loader:
            # Your training logic here
            inputs, targets = batch[:,:-1], batch[:,1:]  # Input is the current token, target is the next token
            inputs, targets = inputs.to(opt.device), targets.to(opt.device)  # Ensure data is on the correct device

            # Forward pass
            outputs = model(inputs, inputmask)
            loss = F.cross_entropy(outputs.view(-1, outputs.size(-1)), targets.view(-1))

            # Backward and optimize
            opt.optimizer.zero_grad()
            loss.backward()
            opt.optimizer.step()
            
            if opt.SGDR == True:
                opt.sched.step()

            total_train_loss += loss.item() * targets.numel()
            total_train_tokens += targets.numel()
            #total_train_loss += loss.item() * inputs.size(0)
            #total_train_tokens += inputs.size(0)

        train_perplexity = torch.exp(torch.tensor(total_train_loss / total_train_tokens))
        print(f"Train perplexity: {train_perplexity}")
        train_losses.append(train_perplexity)
            
        model.eval()  # Set the model to evaluation mode
        total_val_loss = 0
        total_val_tokens = 0

        with torch.no_grad():  # Disable gradient computation
            for batch in valid_loader:
                inputs, targets = batch[:,:-1], batch[:,1:]
                inputs, targets = inputs.to(opt.device), targets.to(opt.device)

                # Forward pass
                # inputmask = torch.triu(torch.ones((1, inputs.size(1), inputs.size(1)), device=opt.device), diagonal=1).bool()
                outputs = model(inputs, inputmask)
                loss = F.cross_entropy(outputs.view(-1, outputs.size(-1)), targets.view(-1))

                total_val_loss += loss.item() * targets.numel()
                total_val_tokens += targets.numel()
                #total_val_loss += loss.item() * inputs.size(0)
                #total_val_tokens += inputs.size(0)
                
            val_perplexity = torch.exp(torch.tensor(total_val_loss / total_val_tokens))
            print(f"Validation perplexity: {val_perplexity}")
            vali_losses.append(val_perplexity)
    
    
def test_model(model, opt, epoch, test_loader):
    print("testing model...")
    model.eval()
    total_test_loss = 0
    total_test_tokens = 0  
    with torch.no_grad():  # Disable gradient computation
        inputmask = torch.triu(torch.ones((1, 511, 511), device=opt.device), diagonal=1).bool()
        inputmask = ~inputmask
        for batch in test_loader:
            inputs, targets = batch[:,:-1], batch[:,1:]
            inputs, targets = inputs.to(opt.device), targets.to(opt.device)

            # Forward pass
            #inputmask = torch.triu(torch.ones((1, inputs.size(1), inputs.size(1)), device=opt.device), diagonal=1).bool()
            outputs = model(inputs, inputmask)
            loss = F.cross_entropy(outputs.view(-1, outputs.size(-1)), targets.view(-1))
            
            total_test_loss += loss.item() * targets.numel()
            total_test_tokens += targets.numel()
            #total_test_loss += loss.item() * inputs.size(0)
            #total_test_tokens += inputs.size(0)
            
        test_perplexity = torch.exp(torch.tensor(total_test_loss / total_test_tokens))
        print(f"Test perplexity: {test_perplexity}")
    model.train()
    


def main():
    
    random.seed(10)
    
    parser = argparse.ArgumentParser()
    parser.add_argument('-no_cuda', action='store_true')
    parser.add_argument('-SGDR', action='store_true')
    parser.add_argument('-epochs', type=int, default=20)
    parser.add_argument('-d_model', type=int, default=512)
    parser.add_argument('-n_layers', type=int, default=6)
    parser.add_argument('-heads', type=int, default=8)
    parser.add_argument('-dropout', type=int, default=0.1)
    parser.add_argument('-batchsize', type=int, default=3)
    parser.add_argument('-printevery', type=int, default=100)
    parser.add_argument('-lr', type=int, default=0.00001)
    parser.add_argument('-seqlen', type=int, default=512)
    parser.add_argument('-threshold', type=int, default=3)
    parser.add_argument('-savename', type=str)    
    parser.add_argument('-loadname', type=str)    
    parser.add_argument('-tied', type=int, default=1)
    parser.add_argument('-dir_name', type=str,default='model')
    parser.add_argument('-norm', type=float, default=2.0)
                
    opt = parser.parse_args()
    opt.verbose = False    
    
    opt.device = 0 if opt.no_cuda is False else -1
    if opt.device == 0:
       assert torch.cuda.is_available()
    opt.device = torch.device("cuda:0")
    #opt.device = torch.device("cpu")
    time_name = time.strftime("%y%m%d_%H%M%S")
    opt.time_name = time_name
    dir_name = "saved/%s" % (opt.dir_name)
    if not os.path.exists(dir_name):
        os.makedirs(dir_name)
    source_name = sys.argv[0]
    dir_name = dir_name + "//"
    opt.dir_name = dir_name
    shutil.copy(source_name,dir_name + source_name)
    opt.log_file = dir_name + "log_file.txt"
    
    print(str(opt))
    tokenizer = GPT2TokenizerFast.from_pretrained("gpt2")
    opt.train = read_corpus('wiki2.train.txt',tokenizer)
    opt.valid = read_corpus('wiki2.valid.txt',tokenizer)
    opt.test = read_corpus('wiki2.test.txt',tokenizer)
    
    
    
    #change 
    def create_fixed_length_sequences(data, sequence_length):
        # Split the data into chunks of `sequence_length`, discarding the remainder
        num_full_batches = len(data) // sequence_length
        # Slice the data to ensure it's a multiple of `sequence_length`
        truncated_length = num_full_batches * sequence_length
        sequences = data[:truncated_length].view(-1, sequence_length)
        return sequences
    
    print(len(opt.train))
    train_dataset = torch.tensor(opt.train)
    train_dataset = create_fixed_length_sequences(train_dataset,opt.seqlen)
    train_dataset = TextDataset(train_dataset)
    print(len(train_dataset))
    
    valid_dataset = torch.tensor(opt.valid)
    valid_dataset = create_fixed_length_sequences(valid_dataset,opt.seqlen)
    valid_dataset = TextDataset(valid_dataset)
    
    test_dataset = torch.tensor(opt.test)
    test_dataset = create_fixed_length_sequences(test_dataset,opt.seqlen)
    test_dataset = TextDataset(test_dataset)
    

    batch_size = opt.batchsize

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, drop_last=True)
    valid_loader = DataLoader(valid_dataset, batch_size=batch_size, shuffle=True, drop_last=True)
    test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=True, drop_last=True)
    
    obs = len(opt.train)
    opt.vocab_size = 50257
    temp = []
    for i in range(opt.vocab_size):
        temp.append(i)
    opt.indices = torch.tensor(temp)
    opt.indices = opt.indices.cuda()
    
    model = get_model(opt,opt.vocab_size,opt.vocab_size)
        
    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])        
    text = 'total params: %d' % (params)
    print(text)

    opt.optimizer = torch.optim.Adam(model.parameters(), lr=opt.lr, betas=(0.9, 0.98), eps=1e-9)
    if opt.SGDR == True:
        opt.sched = CosineWithRestarts(opt.optimizer, T_max=opt.train_len)

    if opt.savename is not None:
        try:
            os.mkdir(opt.savename)
            
        except:
            nothing = 1
    opt.src_pad = 0
    opt.trg_pad = 0
    
    #change     
 
    train_model(model,opt,train_loader,valid_loader)
    test_model(model,opt,-1,test_loader)
    # torch.save(model, 'model.pth')
    os.makedirs(os.path.dirname(opt.save_path), exist_ok=True)
    torch.save(model.state_dict(), opt.save_path)
    
    plt.figure(figsize=(20, 15))
    plt.plot(train_losses, label='Training Loss')
    plt.plot(vali_losses, label='Validation Loss')
    plt.title('Training and Validation Losses')
    plt.xlabel('Epochs')
    plt.ylabel('Loss')
    plt.legend()
    plt.show()
        
        
        
        
        
if __name__ == "__main__":
    main()        