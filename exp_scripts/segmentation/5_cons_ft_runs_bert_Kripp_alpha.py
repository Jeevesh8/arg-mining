import warnings
from itertools import chain
from typing import List, Tuple
warnings.filterwarnings('ignore')

import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim

from arg_mining.utils.krippendorff import krip_alpha
from transformers import BertTokenizer, BertModel
from allennlp.modules.conditional_random_field import ConditionalRandomField as crf

from arg_mining.datasets.cmv_modes import load_dataset, data_config


device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')



def get_tok_model(tokenizer_version, model_version):
    tokenizer = BertTokenizer.from_pretrained(tokenizer_version,
                                              bos_token = "[CLS]",
                                              eos_token = "[SEP]",)

    transformer_model = BertModel.from_pretrained(model_version).to(device)
    
    if tokenizer_version=='bert-base-cased':
        tokenizer.add_tokens(data_config["special_tokens"])
    if model_version=='bert-base-cased':
        transformer_model.resize_token_embeddings(len(tokenizer))
    
    return tokenizer, transformer_model


with open('./Discourse_Markers.txt') as f:
    discourse_markers = [dm.strip() for dm in f.readlines()]


def get_datasets(train_sz, test_sz):
    train_dataset, valid_dataset, test_dataset = load_dataset(tokenizer=tokenizer,
                                                              train_sz=train_sz,
                                                              test_sz=test_sz,
                                                              shuffle=True,
                                                              mask_tokens=discourse_markers,)
    return train_dataset, valid_dataset, test_dataset


def split_encoding(tokenized_thread: List[int], 
                   split_on: List[int], 
                   eos_token_id: int) -> List[List[int]]:
    """Splits tokenized_thread into multiple lists at each occurance of 
    a token_id specified in split_on or the eos_token_id.
    
    1. The eos_token_id is retained in the last splitted component.
    2. Each matched token_id from split_on is retained in the component that 
       follows it.
    """
    splitted = [[]]
    for elem in tokenized_thread:
        if elem in split_on:
            splitted.append([])
        splitted[-1].append(elem)
        if elem == eos_token_id:
            break
    return splitted

def pad_batch(elems: List[List[int]], pad_token_id: int) -> List[List[int]]:
    """Pads all lists in elems to the maximum list length of any list in 
    elems. Pads with pad_token_id.
    """
    max_len = max([len(elem) for elem in elems])
    return [elem+[pad_token_id]*(max_len-len(elem)) for elem in elems]

def get_comment_wise_dataset(dataset,
                             max_len: int=512,
                             batch_size: int=8) -> Tuple[List[List[int]], 
                                                         List[List[int]], 
                                                         List[List[int]]]:
    """
    Args:
        dataset:     A numpy iterator dataset for threads, as returned from 
                     get_datasets() function above.
        max_len:     Maximum length at which to truncate any comment.
        batch_size:  Number of comments in a batch
    
    Returns:
        A tuple having batched & padded(to max. length in batch) tokenized threads, 
        masked threads, and component type labels; where each element corresponds
        to a comment in some thread.
    
    NOTE:
        This function removes the extra num_devices dimension from the elements 
        of dataset provided.
    """
    user_token_indices = tokenizer.encode("[UNU]"+"".join([f"[USER{i}]" for i in range(data_config["max_users"])]))[1:-1]
    comment_wise_tokenized_threads = []
    comment_wise_masked_threads = []
    comment_wise_comp_type_labels = []

    for (tokenized_threads, masked_threads, comp_type_labels, _ ) in dataset:
        tokenized_threads = np.squeeze(tokenized_threads, axis=0).tolist()
        masked_threads = np.squeeze(masked_threads, axis=0).tolist()
        comp_type_labels = np.squeeze(comp_type_labels, axis=0).tolist()

        for tokenized_thread, masked_thread, comp_type_label in zip(tokenized_threads, masked_threads, comp_type_labels):
            splitted_encodings = split_encoding(tokenized_thread, user_token_indices, tokenizer.eos_token_id)
            for elem in splitted_encodings:
                comment_wise_tokenized_threads.append(elem)
                comment_wise_masked_threads.append(masked_thread[:len(elem)])
                comment_wise_comp_type_labels.append(comp_type_label[:len(elem)])
                masked_thread, comp_type_label = masked_thread[len(elem):], comp_type_label[len(elem):]
    i = 0
    cw_tokenized_threads, cw_masked_threads, cw_comp_type_labels = [], [], []
    while i<len(comment_wise_tokenized_threads):
         cw_tokenized_threads.append(comment_wise_tokenized_threads[i][:max_len])
         cw_masked_threads.append(comment_wise_masked_threads[i][:max_len])
         cw_comp_type_labels.append(comment_wise_comp_type_labels[i][:max_len])
         i += 1
         
         if i%batch_size==0:
             yield (pad_batch(cw_tokenized_threads, tokenizer.pad_token_id), 
                    pad_batch(cw_masked_threads, tokenizer.pad_token_id),
                    pad_batch(cw_comp_type_labels, data_config["pad_for"]["comp_type_labels"]))
            
             cw_tokenized_threads, cw_masked_threads, cw_comp_type_labels = [], [], []

### Define layers for a Linear-Chain-CRF

ac_dict = data_config["arg_components"]

allowed_transitions =([(ac_dict["B-C"], ac_dict["I-C"]), 
                       (ac_dict["B-P"], ac_dict["I-P"])] + 
                      [(ac_dict["I-C"], ac_dict[ct]) 
                        for ct in ["I-C", "B-C", "B-P", "O"]] +
                      [(ac_dict["I-P"], ac_dict[ct]) 
                        for ct in ["I-P", "B-C", "B-P", "O"]] +
                      [(ac_dict["O"], ac_dict[ct]) 
                        for ct in ["O", "B-C", "B-P"]])
                    
def get_crf_head():
    linear_layer = nn.Linear(transformer_model.config.hidden_size,
                             len(ac_dict)).to(device)

    crf_layer = crf(num_tags=len(ac_dict),
                    constraints=allowed_transitions,
                    include_start_end_transitions=False).to(device)

    return linear_layer, crf_layer


cross_entropy_layer = nn.CrossEntropyLoss(weight=torch.log(torch.tensor([3.3102, 61.4809, 3.6832, 49.6827, 2.5639], 
                                                                        device=device)), reduction='none')

"""### Loss and Prediction Function"""

def compute(batch: Tuple[torch.Tensor, torch.Tensor, torch.Tensor],
            preds: bool=False, cross_entropy: bool=True):
    """
    Args:
        batch:  A tuple having tokenized thread of shape [batch_size, seq_len],
                component type labels of shape [batch_size, seq_len], and a global
                attention mask for Longformer, of the same shape.
        
        preds:  If True, returns a List(of batch_size size) of Tuples of form 
                (tag_sequence, viterbi_score) where the tag_sequence is the 
                viterbi-decoded sequence, for the corresponding sample in the batch.
        
        cross_entropy:  This argument will only be used if preds=False, i.e., if 
                        loss is being calculated. If True, then cross entropy loss
                        will also be added to the output loss.
    
    Returns:
        Either the predicted sequences with their scores for each element in the batch
        (if preds is True), or the loss value summed over all elements of the batch
        (if preds is False).
    """
    tokenized_threads, token_type_ids, comp_type_labels = batch
    
    pad_mask = torch.where(tokenized_threads!=tokenizer.pad_token_id, 1, 0)
    
    logits = linear_layer(transformer_model(input_ids=tokenized_threads,
                                            attention_mask=pad_mask,).last_hidden_state)
    
    if preds:
        return crf_layer.viterbi_tags(logits, pad_mask)
    
    log_likelihood = crf_layer(logits, comp_type_labels, pad_mask)
    
    if cross_entropy:
        logits = logits.reshape(-1, logits.shape[-1])
        
        pad_mask, comp_type_labels = pad_mask.reshape(-1), comp_type_labels.reshape(-1)
        
        ce_loss = torch.sum(pad_mask*cross_entropy_layer(logits, comp_type_labels))
        
        return ce_loss - log_likelihood

    return -log_likelihood


"""### Training And Evaluation Loops"""

def train(dataset):
    accumulate_over = 4
    
    optimizer.zero_grad()

    i=0
    for (tokenized_threads, masked_threads, comp_type_labels) in get_comment_wise_dataset(dataset):
        
        #Cast to PyTorch tensor
        tokenized_threads = torch.tensor(tokenized_threads, device=device)
        masked_threads = torch.tensor(masked_threads, device=device)
        comp_type_labels = torch.tensor(comp_type_labels, device=device, dtype=torch.long)
        
        loss = compute((tokenized_threads,
                        torch.where(masked_threads==tokenizer.mask_token_id, 1, 0), 
                        comp_type_labels,))/data_config["batch_size"]
        
        print("Loss: ", loss)
        loss.backward()
        
        if i%accumulate_over==accumulate_over-1:
            optimizer.step()
            optimizer.zero_grad()
        
        i += 1

    optimizer.step()

def evaluate(dataset, metric):
    
    int_to_labels = {v:k for k, v in ac_dict.items()}
    with torch.no_grad():
        for tokenized_threads, masked_threads, comp_type_labels in get_comment_wise_dataset(dataset):
        
            #Cast to PyTorch tensor
            tokenized_threads = torch.tensor(tokenized_threads, device=device)
            masked_threads = torch.tensor(masked_threads, device=device)
            comp_type_labels = torch.tensor(comp_type_labels, device=device)
            
            preds = compute((tokenized_threads,
                            torch.where(masked_threads==tokenizer.mask_token_id, 1, 0), 
                            comp_type_labels,), preds=True)
            
            lengths = torch.sum(torch.where(tokenized_threads!=tokenizer.pad_token_id, 1, 0), 
                                axis=-1)
            
            preds = [ [int_to_labels[pred] for pred in pred[0][:lengths[i]]]
                    for i, pred in enumerate(preds)
                    ]
            
            refs = [ [int_to_labels[ref] for ref in labels[:lengths[i]]]
                    for i, labels in enumerate(comp_type_labels.cpu().tolist())
                ]
            
            metric.add_batch(predictions=preds, 
                            references=refs,
                            tokenized_threads=tokenized_threads.cpu().tolist())
        
        print("\t\t\t\t Metric computations:", metric.compute())

"""### Final Training"""

n_epochs = 20
n_runs = 5

for (tokenizer_version, model_version) in [('bert-base-cased', 'bert-base-cased'),
                                           ('arg_mining/smlm_pretrained_iter2_0/tokenizer', 'arg_mining/smlm_pretrained_iter2_0/model'),
                                           ('arg_mining/smlm_pretrained_iter5_0/tokenizer', 'arg_mining/smlm_pretrained_iter5_0/model'),]:

    print("Tokenizer:", tokenizer_version, "Model:", model_version)

    for (train_sz, test_sz) in [(80,20),(50,50)]:

        print("\tTrain size:", train_sz, "Test size:", test_sz)

        for run in range(n_runs):
            print(f"\n\n\t\t-------------RUN {run+1}-----------")

            tokenizer, transformer_model = get_tok_model(tokenizer_version, model_version)

            linear_layer, crf_layer = get_crf_head()

            optimizer = optim.Adam(params = chain(transformer_model.parameters(),
                                      linear_layer.parameters(),
                                      crf_layer.parameters()),
                                   lr = 2e-5,)

            train_dataset, _, test_dataset = get_datasets(train_sz, test_sz)
            train_dataset = [elem for elem in train_dataset]
            test_dataset = [elem for elem in test_dataset]
            
            metric = krip_alpha(tokenizer)

            for epoch in range(n_epochs):
                print(f"\t\t\t------------EPOCH {epoch+1}---------------")
                train(train_dataset)
                evaluate(test_dataset, metric)

            del tokenizer, transformer_model, linear_layer, crf_layer
