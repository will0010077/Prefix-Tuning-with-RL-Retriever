# import numpy as np
import sys
sys.path.append('..')
import torch
from torch import Tensor, nn
from torch.utils.data import DataLoader
import torch.nn.functional as F
import math
from DocBuilder.Retriever_k_means import inner
from DocBuilder.Retriever_k_means import doc_retriever
from DocBuilder.LexMAE import lex_retriever
from DocBuilder.utils import sparse_retrieve_rep, tensor_retuen_type
from DatasetLoader.collate_func import collate
from LM.llama_reader import EncTunedLM
from metric.reward import BLEU_score, Bert_score,ROUGE_score
import random
import config
import torch.optim as optim
from transformers import BertModel, BertConfig, BertTokenizer, RobertaModel, RobertaTokenizer, RobertaForMaskedLM
from torch.distributions import Categorical
from config import agent_size_config
from tqdm import tqdm
from itertools import chain

    
def generate_segments(text:str, window_size, step)-> list[str]:

    text = text.split()
    segment_list=[]

    for i in range(0, max(len(text)-window_size,1), step):
        segment_data = text[max(0, min(i, len(text)-window_size)):i+window_size]
        # print(segment_data.shape)
        segment_list.append(" ".join(segment_data))
    return  segment_list
class LLMEnv_batch_version:
    def __init__(self, dataset, LM: EncTunedLM, ret: lex_retriever, action_space_size, history_len=24, batch_size=8, shuffle = True, step_size=15):
        self.dataset = dataset  # List of tuples (x, y)
        self.action_space_size = action_space_size
        self.history_len = history_len
        self.LM = LM
        self.eos_id = self.LM.tokenizer.eos_token_id
        self.ret = ret
        self.current_index = 0
        self.collate = collate()
        self.batch_size = batch_size  # Set batch size as length of dataset
        self.action_verb=[" retrieve", " proceed", " rewrite"]
        self.step_size=step_size
        
        
        self.x = [None] * self.batch_size
        self.y = [None] * self.batch_size
        self.document = [None] * self.batch_size
        self.input_ids = [None] * self.batch_size
        self.attention_mask = [None] * self.batch_size
        self.embedding = [None] * self.batch_size
        
        self.d_t = [None] * self.batch_size
        self.basic_reward = [None] * self.batch_size
        self.probs = [None] * self.batch_size
        self.probs_halulu = [None] * self.batch_size
        self.reward = [None] * self.batch_size
        self.revise_reward = [None] * self.batch_size
        self.hat_y_t = [None] * self.batch_size
        self.response_cache = [None] * self.batch_size
        self.n = [None] * self.batch_size
        self.done = [None] * self.batch_size
        self.steps = [None] * self.batch_size
        self.last_action = [None] * self.batch_size
        self.action_history = [None] * self.batch_size
        self.last_proceed = [-1] * self.batch_size
        self.shuffle = shuffle

    def reset(self, idx=None):
        if idx is None:
            for i in range(self.batch_size):
                self._reset_idx(i)
            return [self.get_state(i) for i in range(self.batch_size)]
        else:
            self._reset_idx(idx)
            return self.get_state(idx)
    @torch.no_grad()
    def _build_embedding(self, idx):
        self.document[idx] = generate_segments(self.document[idx],96,64)[:256]
        self.input_ids[idx] = []
        self.attention_mask[idx] = []
        self.embedding[idx] = []
        for i in range(0,len(self.document[idx]), 32):
            tokens = self.collate.datatokenizer(self.document[idx][i:i+32], padding = True, truncation=True, max_length=256, return_tensors="pt", add_special_tokens=False).to(self.ret.device)
            self.input_ids[idx].extend(tokens.input_ids)
            self.attention_mask[idx].extend(tokens.attention_mask)
            self.embedding[idx].extend(self.ret.forward(tokens)) #(N,d)
        self.embedding[idx] = torch.stack(self.embedding[idx])
    @torch.no_grad()
    def retrieve(self, ids:list, x:list[str]):
        query = self.ret.tokenizer(x, return_tensors="pt", padding=True, truncation=True).to(self.ret.device)
        query = self.ret.forward(query)#(b,d)
        retrieved = []
        for idx, q in zip(ids, query):
            topk = torch.argmax(q[None] @ self.embedding[idx].T, dim=-1)[0]#(1,1)->()
            retrieved.append(self.input_ids[idx][topk][:sum(self.attention_mask[idx][topk])])
        return retrieved
        
    def _reset_idx(self, idx):
        if self.shuffle:
            self.current_data = self.dataset[random.randrange(len(self.dataset))]
        else:
            self.current_data = self.dataset[self.current_index%len(self.dataset)]
        self.current_index+=1
        self.x[idx], self.y[idx], self.document[idx] = self.current_data
        self._build_embedding(idx)
        self.y[idx] = self.y[idx].split(' ')
        chunk_size = 10
        self.y[idx] = [' '.join(self.y[idx][i:i + chunk_size]) for i in range(0, len(self.y[idx]), chunk_size)]
        self.d_t[idx] = self.retrieve([idx], self.x[idx])[0]
        self.basic_reward[idx] = Bert_score([self.get_basic_response(self.x[idx], " ".join(self.y[idx]), self.d_t[idx])[0]], [" ".join(self.y[idx])])[0]
        self.reward[idx] = []
        self.revise_reward[idx] = []
        self.hat_y_t[idx] = None
        self.response_cache[idx] = [self.hat_y_t[idx]]
        self.n[idx] = -1  # Initialize n to -1
        self.done[idx] = False
        self.steps[idx] = 0
        self.last_action[idx] = -1
        self.last_proceed[idx] = -1
        self.action_history[idx] = []
        
    def get_state(self, idx) -> str:
        state = self.collate.state_templete(
            self.x[idx],
            self.cat_response(self.response_cache[idx][-self.history_len:]),
            [self.action_verb[i] for i in self.action_history[idx]],
            self.d_t[idx]
        )
        return state

    def step(self, actions:Tensor):
        rewards = [0] * self.batch_size
        next_states = []

        retrieve_indices = []
        proceed_indices = []
        rewrite_indices = []
        self.actions = actions.clone()
        for i, action in enumerate(actions):
            if not self.done[i]:
                self.action_history[i].append(action)
                if action == 0:  # Retrieve Document
                    if self.last_action[i] != 0:
                        retrieve_indices.append(i)
                elif action == 1:  # Proceed Response
                    if self.n[i] + 1 < len(self.y[i]):
                        self.n[i] += 1
                        self.last_proceed[i] = self.steps[i]
                        proceed_indices.append(i)
                    else:
                        self.done[i]=True
                        self.actions[i]=-1
                elif action == 2:  # Rewrite Current Response
                    if self.n[i] > -1:
                        self.response_cache[i].pop()
                        rewrite_indices.append(i)

        # Process Retrieve Document actions
        
        if len(retrieve_indices)>0:
            q_t = [self.construct_query(i) for i in retrieve_indices]
            d_t = self.retrieve(retrieve_indices, q_t)
            for idx, i in enumerate(retrieve_indices):
                self.d_t[i] = d_t[idx]

        # Process Proceed and Rewrite actions in a batch
        batch_indices = proceed_indices + rewrite_indices
        if batch_indices:
            responses, token_prob_input, token_prob_resampled = self.get_next_response(batch_indices)
            for idx, i in enumerate(batch_indices):
                self.hat_y_t[i] = responses[idx]
                self.probs[i] = token_prob_input[idx]
                self.probs_halulu[i] = token_prob_resampled[idx]
                self.response_cache[i].append(self.hat_y_t[i])

        for i in range(self.batch_size):
            if self.steps[i]>3*len(self.y[i]):
                self.done[i]=True
        rewards = self.compute_reward()
        for i in range(self.batch_size):
            next_states.append(self.get_state(i))
            self.steps[i] += 1
        self.last_action = actions.clone()

        return next_states, rewards, self.done, {}

    def compute_reward(self, ):
        rewards = [0]*self.batch_size
        proceed_indices = []
        rewrite_indices = []
        cands=[]
        refs=[]
        bert_idx=[]
        for idx in range(self.batch_size):
            if self.done[idx]:
                if self.n[idx] > -1:
                    cands.append(self.cat_response(self.response_cache[idx]))
                    refs.append(" ".join(self.y[idx]))
                    bert_idx.append(idx)
                    rewards[idx] += - self.basic_reward[idx]
                    rewards[idx] += 0.1 * ((self.n[idx] + 1) / len(self.y[idx])) ** 2
                    rewards[idx] = float(rewards[idx])
                    
                        
            elif self.actions[idx] == 0:
                # retrieval score
                if self.action_history[idx][-1]!=0 and self.action_history[idx].count(0)<len(self.y[idx]):
                    cands.append(self.ret.tokenizer.decode(self.d_t[idx], skip_special_tokens=True))
                    refs.append(" ".join(self.y[idx]))
                    bert_idx.append(idx)
            elif self.actions[idx] == 1:
                rewards[idx] += 0.2*self.probs[idx].exp().mean()
                rewards[idx] += 0.02*self.probs_halulu[idx].exp().mean() 
                
            elif self.actions[idx] == 2:
                if self.n[idx] > -1:
                    rewards[idx] += 0.2*self.probs[idx].exp().mean() 
                    rewards[idx] += 0.02*self.probs_halulu[idx].exp().mean() 
                else:
                    rewards[idx] -= 0.05*len(self.y[idx])
        
        if cands:
            bert = Bert_score(cands, refs)
            for i, idx in enumerate(bert_idx):
                rewards[idx] += bert[i]
            
        # batch_indices = proceed_indices + rewrite_indices
        # if batch_indices:
        #     cands = [self.cat_response(self.response_cache[idx][-1:]) for idx in batch_indices if self.n[idx]>-1]
        #     refs = [self.y[idx][self.n[idx]] for idx in batch_indices if self.n[idx]>-1]
        #     batch_bert =  list(Bert_score(cands, refs))
        #     for i, idx in enumerate(batch_indices):
        #         rewards[idx]+=batch_bert[i]
        for idx in range(self.batch_size):
            if rewards[idx]!=rewards[idx]:
                print("reward NAN!!")
                self.done[idx] = True
                rewards[idx]=0
            self.reward[idx].append(rewards[idx])
            if self.done[idx]:
                # Spread the final reward across previous actions
                # Is it good? Or just put it at the final?
                # rewards[idx] /= (len(self.reward[idx])+1)
                # for i in self.reward[idx]:
                #     self.reward[idx] += rewards[idx]
                    
                # Spread the reward of whole trajectory across previous actions, assign to revised_reward
                for i, (r, a) in enumerate(zip(self.reward[idx], self.action_history[idx])):
                    if i==len(self.reward[idx])-1:
                        self.revise_reward[idx].append(r)
                    elif a==0:
                        self.revise_reward[idx].append(r/len(self.y[idx]))
                    elif a==1:
                        self.revise_reward[idx].append(r/len(self.y[idx]))
                    elif a==2:
                        count=1
                        for j in reversed(range(i)):
                            if self.action_history[idx][j]>0:# counting 1 and 2
                                count+=1
                            if self.action_history[idx][j]==1:# until last proceed
                                break
                        r = r/len(self.y[idx])/count-0.0005
                        self.revise_reward[idx].append(r)
                        for j in reversed(range(i)):
                            if self.action_history[idx][j]>0:
                                self.revise_reward[idx][j] = r
            
        return rewards

    def construct_query(self, idx):
        return self.x[idx] + self.cat_response(self.response_cache[idx][-self.history_len:])

    def cat_response(self, cache: list[Tensor]) -> str:
        if cache[0] is None:
            cache = cache[1:]
        if len(cache) == 0:
            return ""
        s = self.LM.tokenizer.decode(torch.cat(cache), skip_special_tokens=True)
        return s

    def get_next_response(self, indices):
        response = [self.cat_response(self.response_cache[i]) for i in indices]
        messages = [" ".join(self.collate.templete(self.x[i], response[idx])) for idx, i in enumerate(indices)]
        answers = [self.y[i][self.n[i]] for i in indices]
        # d_t = torch.stack([self.d_t[i] for i in indices])#need to padding
        d_t = self.ret.tokenizer.batch_decode([self.d_t[i] for i in indices], skip_special_tokens=True)
        d_t = self.ret.tokenizer(d_t, return_tensors="pt", padding=True).to(self.LM.device)
        # d_t = tensor_retuen_type(input_ids=d_t, attention_mask=torch.ones_like(d_t)).to(self.LM.device)

        responses, token_prob_input, token_prob_resampled = self.LM.pseudo_generate(messages, answers, Doc_tokens=d_t, temperture=0.2, return_prob=True, decode=False)
        return responses, token_prob_input, token_prob_resampled

    def get_basic_response(self, x, y, d_t):
        messages, answer = self.collate.templete(x, "")
        d_t = tensor_retuen_type(input_ids=d_t[None], attention_mask=torch.ones_like(d_t[None])).to(self.LM.device)
        response = self.LM.pseudo_generate(messages, y, Doc_tokens=d_t, temperture=0.2, return_prob=False, decode=True)
        return response



class LLMEnv_test(LLMEnv_batch_version):
    
    def step(self, actions:Tensor):
        rewards = [0] * self.batch_size
        next_states = []

        retrieve_indices = []
        proceed_indices = []
        rewrite_indices = []
        self.actions = actions.clone()
        for i, action in enumerate(actions):
            if not self.done[i]:
                self.action_history[i].append(action)
                if action == 0:  # Retrieve Document
                    if self.last_action[i] != 0:
                        retrieve_indices.append(i)
                elif action == 1:  # Proceed Response
                    if self.hat_y_t[i] is not None and self.eos_id in self.hat_y_t[i]:
                        self.done[i] = True
                        continue
                    proceed_indices.append(i)
                elif action == 2:  # Rewrite Current Response
                    if self.n[i] > -1:
                        self.response_cache[i].pop()
                        rewrite_indices.append(i)

        # Process Retrieve Document actions
        
        if len(retrieve_indices)>0:
            q_t = [self.construct_query(i) for i in retrieve_indices]
            d_t= self.retrieve(retrieve_indices, q_t)
            for idx, i in enumerate(retrieve_indices):
                self.d_t[i] = d_t[idx]

        # Process Proceed and Rewrite actions in a batch
        batch_indices = proceed_indices + rewrite_indices
        if batch_indices:
            responses = self.get_next_response(batch_indices)
            for idx, i in enumerate(batch_indices):
                self.hat_y_t[i] = responses[idx]
                self.response_cache[i].append(self.hat_y_t[i])

        rewards = self.compute_reward()
        for i in range(self.batch_size):
            next_states.append(self.get_state(i))
            self.steps[i] += 1
        self.last_action = actions.clone()

        return next_states, rewards, self.done, {}

    def compute_reward(self):
        return [0]*self.batch_size
    
    def get_next_response(self, indices):
        response = [self.cat_response(self.response_cache[i]) for i in indices]
        messages = [" ".join(self.collate.templete(self.x[i], response[idx])) for idx, i in enumerate(indices)]
        d_t = self.ret.tokenizer.batch_decode([self.d_t[i] for i in indices], skip_special_tokens=True)
        d_t = self.ret.tokenizer(d_t, return_tensors="pt", padding=True).to(self.LM.device)

        responses = self.LM.generate(messages, Doc_tokens=d_t, max_new_tokens=self.step_size, decode=False)
        return responses
class Orginal_Env(LLMEnv_test):
    def get_next_response(self, indices):
        response = [self.cat_response(self.response_cache[i]) for i in indices]
        messages = [" ".join(self.collate.templete(self.x[i], response[idx])) for idx, i in enumerate(indices)]
        d_t = self.ret.tokenizer.batch_decode([self.d_t[i] for i in indices], skip_special_tokens=True)

        messages = [messages[j][:57]+ d_t[j]+ messages[j][57:] for j in range(len(messages))]
        responses = self.LM.generate(messages, Doc_tokens=None, max_new_tokens=self.step_size, decode=False)
        return responses
    
    
class BertAgentCritic(nn.Module):
    def __init__(self, model_config, action_space_size):
        super(BertAgentCritic, self).__init__()
        self.bert = RobertaModel.from_pretrained(config.roberta_dir, torch_dtype=torch.bfloat16).to(torch.bfloat16)
        embedding = self.bert.embeddings.position_embeddings

        new_embedding = torch.nn.Embedding(768+2, embedding.embedding_dim, dtype = torch.bfloat16)
        new_embedding.weight.data[:len(embedding.weight),:]=embedding.weight.data
        self.bert.embeddings.position_embeddings = new_embedding
        self.bert.embeddings.register_buffer(
            "position_ids", torch.arange(768+2).expand((1, -1)), persistent=False
        )
        self.bert.embeddings.register_buffer(
            "token_type_ids", torch.zeros(self.bert.embeddings.position_ids.size(), dtype=torch.long), persistent=False
        )
        self.tokenizer = RobertaTokenizer.from_pretrained(config.roberta_dir)
        self.action_head = nn.Linear(self.bert.config.hidden_size, action_space_size)
        self.value_head = nn.Linear(self.bert.config.hidden_size, 1)
        self.action_space_size = action_space_size
        self.max_token_length = self.bert.embeddings.position_embeddings.num_embeddings
        self.prompt_text = "These are keywords for summary: "
        # Add special tokens to the tokenizer
        special_tokens_dict = {'additional_special_tokens': ['[actor_head]', '[value_head]']}
        self.tokenizer.add_special_tokens(special_tokens_dict)
        self.bert.resize_token_embeddings(len(self.tokenizer))
        self.special_tokens = self.tokenizer.convert_tokens_to_ids(['[actor_head]', '[value_head]'])
        self.special_tokens = torch.tensor(self.special_tokens).unsqueeze(0)
        
        self.prompt_inputs = self.tokenizer(self.prompt_text, return_tensors="pt", add_special_tokens =False)
        self.prompt_len = self.prompt_inputs['input_ids'].size(1)

    def forward(self, state=None, inputs=None):
        assert (state is not None and inputs is None) or (state is None and inputs is not None)
        if inputs is None:
        # Tokenize the state and the prompt text
            inputs = self.tokenizer(state, return_tensors="pt", padding=True, truncation=True).to(self.bert.device)
            
            # Combine the prompt text and state input ids and attention masks
        inputs = {
                'input_ids': inputs['input_ids'],
                'attention_mask': inputs['attention_mask']
        }
        
        batch_size = inputs['input_ids'].size(0)
        
        
        # Calculate the total length with the mask tokens, special tokens, and prompt text
        total_length = self.prompt_len + 2 + inputs['input_ids'].size(1)
        
        # If total length exceeds max_token_length, truncate the input sequence
        if total_length > self.max_token_length:
            truncate_length = total_length - self.max_token_length
            inputs['input_ids'] = inputs['input_ids'][:, :-truncate_length]
            inputs['attention_mask'] = inputs['attention_mask'][:, :-truncate_length]
        
        # Concatenate prompt text, mask tokens, and special tokens with the input sequence
        inputs['input_ids'] = torch.cat([self.prompt_inputs['input_ids'].to(self.bert.device).repeat(batch_size, 1), self.special_tokens.to(self.bert.device).repeat(batch_size, 1), inputs['input_ids']], dim=1)
        inputs['attention_mask'] = torch.cat([torch.ones((batch_size,  self.prompt_len+ 2), dtype=torch.long).to(self.bert.device), inputs['attention_mask']], dim=1)
        
        outputs = self.bert(**inputs, output_hidden_states = True)
        
        # Handle special tokens for action and value heads
        actor_head_output = outputs.hidden_states[-1][:, self.prompt_len, :]  # Shape: (batch_size, hidden_size)
        value_head_output = outputs.hidden_states[-1][:, self.prompt_len + 1, :]  # Shape: (batch_size, hidden_size)
        
        action_logits_special = self.action_head(actor_head_output).float()  # Shape: (batch_size, action_space_size)
        state_value_special = self.value_head(value_head_output)[..., 0].float()  # Shape: (batch_size,)

        return action_logits_special, state_value_special

class PPOTrainer:
    def __init__(self, model:BertAgentCritic, optimizer:torch.optim.Optimizer, gamma=0.99, clip_epsilon=0.2, lambd=0.95, update_epochs=4, batch_size=32, grad_step = 4):
        self.model = model
        self.optimizer = optimizer
        self.gamma = gamma
        self.clip_epsilon = clip_epsilon
        self.lambd = lambd
        self.update_epochs = update_epochs
        self.batch_size = batch_size
        self.grad_step = grad_step
        
        self.action_coef=1
        self.value_coef=2**1
        
        self.max_entr = torch.tensor(2**-6)
        self.min_entr = torch.tensor(2**-10)
        self.entropy_coef=torch.tensor(2**-7)
        self.sep = collate().datatokenizer.sep_token

    def ppo_loss(self, action_logp, action_dist:Categorical, batch_actions, advantages, returns, values):
        # old_log_probs shape: (batch_size,)
        # batch_action shape: (batch_size,)
        # advantages shape: (batch_size,)
        # returns shape: (batch_size,)
        # values shape: (batch_size,)
        new_action_logp = action_dist.log_prob(batch_actions)
        ratios = torch.exp(new_action_logp - action_logp)  # Shape: (batch_size,)
        
        # need to broadcast `advantages` to match the shape of `ratios`
        
        surr1 = ratios * advantages  # Shape: (batch_size,)
        surr2 = torch.clamp(ratios, 1 - self.clip_epsilon, 1 + self.clip_epsilon) * advantages  # Shape: (batch_size,)
        actor_loss = -torch.min(surr1, surr2).mean()  # Shape: scalar


        critic_loss = F.huber_loss(values, returns, "mean", 1.0)  # Shape: scalar
        action_entropy:Tensor = action_dist.entropy().mean() #scalar
        
        return actor_loss, critic_loss, - action_entropy  # Shape: scalar

    def compute_gae(self, rewards, values, dones, next_value):
        # rewards shape: (sequence_length,)
        # values shape: (sequence_length,)
        # dones shape: (sequence_length,)
        # next_value shape: scalar

        values = values + (next_value,)  # Shape: (sequence_length + 1,)
        gae = 0
        returns = []
        for step in reversed(range(len(rewards))):
            delta = rewards[step] + self.gamma * values[step + 1] * (1 - dones[step]) - values[step]  # Shape: scalar
            gae = delta + self.gamma * self.lambd * (1 - dones[step]) * gae  # Shape: scalar
            returns.insert(0, gae + values[step])  # Shape: scalar
        return returns  # Shape: (sequence_length,)
    def f(self,batch):
        batch_states, batch_actions, batch_old_log_probs, batch_returns, batch_advantages = zip(*batch)
        
        batch_token = self.model.tokenizer(batch_states, return_tensors = "pt", padding = True, truncation=True, return_special_tokens_mask =True)
        questions = [batch_states[i].split(self.sep)[1] for i in range(len(batch_states))]
        questions_token = self.model.tokenizer(questions, return_tensors = "pt", padding = True, truncation=True, return_special_tokens_mask =True)
        batch_actions = torch.stack(batch_actions)
        batch_old_log_probs = torch.stack(batch_old_log_probs)
        batch_returns = torch.stack(batch_returns)
        batch_advantages = torch.stack(batch_advantages)
        return questions_token, batch_token, batch_actions, batch_old_log_probs, batch_returns, batch_advantages
    def update(self, memory):
        old_states, old_actions, old_log_probs, rewards, dones, values = zip(*memory)
        returns = self.compute_gae(rewards, values, dones, next_value=0)

        old_states = old_states # Shape: (memory_size, state_size)
        old_actions = torch.tensor(old_actions, dtype=torch.long)  # Shape: (memory_size,)
        old_log_probs = torch.tensor(old_log_probs, dtype=torch.float32)  # Shape: (memory_size,)
        returns = torch.tensor(returns, dtype=torch.float32)  # Shape: (memory_size,)
        values = torch.tensor(values, dtype=torch.float32)  # Shape: (memory_size,)
        torch.save(returns, "save/return.pt")
        torch.save(values, "save/value.pt")
        advantages = returns - values  # Shape: (memory_size,)
        advantages = F.normalize(advantages, dim=0)
        loader = DataLoader([*zip(old_states, old_actions, old_log_probs, returns, advantages)], self.batch_size, True, collate_fn=self.f, num_workers=1, pin_memory = True, persistent_workers=True, drop_last=True)
        step = 0
        bar = tqdm(total=self.update_epochs*len(loader), ncols=0)
        self.optimizer.zero_grad()
        for _ in range(self.update_epochs):
            for questions_token, batch_token, batch_actions, batch_old_log_probs, batch_returns, batch_advantages in loader:
                step+=1
                batch_token = batch_token.to(self.model.bert.device)  # Shape: (batch_size, n)
                batch_actions = batch_actions.to(self.model.bert.device)  # Shape: (batch_size,)
                batch_old_log_probs = batch_old_log_probs.to(self.model.bert.device)  # Shape: (batch_size,)
                batch_returns = batch_returns.to(self.model.bert.device)  # Shape: (batch_size,)
                batch_advantages = batch_advantages.to(self.model.bert.device)  # Shape: (batch_size,)
                action_logits, state_values = self.model.forward(inputs = batch_token)  # logits shape: (batch_size, action_space_size), state_values shape: (batch_size, 1)
                action_dist = Categorical(logits=action_logits)  # Shape: (batch_size,)
                
                # maximize the state's token for query dist
                questions_token = questions_token.to(self.model.bert.device)
                
                actor_loss, value_loss, a_entropy_loss = self.ppo_loss(batch_old_log_probs, action_dist, batch_actions, batch_advantages, batch_returns, state_values)  # Shape: scalar
                if -a_entropy_loss>0.8:
                    self.entropy_coef/=1.05
                else:
                    self.entropy_coef*=1.05
                self.entropy_coef = torch.clamp(self.entropy_coef, self.min_entr, self.max_entr)
                    
                loss:Tensor = self.action_coef*actor_loss+ self.value_coef*value_loss+ self.entropy_coef*a_entropy_loss# + 0.001*query_norm_loss
                loss.backward()
                if step%self.grad_step==0:
                    torch.nn.utils.clip_grad_norm_(chain(*[self.optimizer.param_groups[param_i]['params'] for param_i in [0,1,2]]), 1.0)
                    self.optimizer.step()
            bar.set_postfix_str(f"ac: {actor_loss:.3f}, value: {value_loss:.3f}, entropy: {-a_entropy_loss:.3f}")
            bar.update(len(loader))


class LLMEnv:
    def __init__(self, dataset, LM:EncTunedLM, ret:doc_retriever, action_space_size, history_len = 6):
        self.dataset = dataset  # List of tuples (x, y)
        self.action_space_size = action_space_size
        self.history_len = history_len
        self.LM = LM
        self.eos_id = self.LM.tokenizer.eos_token_id
        self.ret = ret
        self.current_index = 0
        self.collate = collate()

    def reset(self):
        self.current_data = self.dataset[random.randrange(len(self.dataset))]
        self.current_index+=1
        self.x, self.y = self.current_data
        self.y:list[str] = self.y.split(' ')
        chunk_size = 10
        self.y = [' '.join(self.y[i:i+chunk_size]) for i in range(0,len(self.y), chunk_size)]
        self.d_t, zt = self.ret.retrieve(self.x, k=1, num_search=4)
        self.d_t = self.d_t.squeeze(1)
        self.basic_reward = Bert_score([self.get_basic_response(self.x, " ".join(self.y))[0]], [" ".join(self.y)])[0]
        self.halulu = []
        self.revise_reward = []
        self.hat_y_t = None
        self.response_cache = [self.hat_y_t]
        self.n = -1  # Initialize n to -1
        self.done = False
        self.steps = 0
        self.last_action=-1
        self.action_history = []
        return self.get_state()

    def get_state(self)->str:
        state = self.collate.state_templete(self.x, self.cat_response(self.response_cache[-self.history_len:]), self.action_history)
        return state

    def step(self, action):
        reward = 0
        self.action = action
        if action == 0:  # Retrieve Document
            self.action_history.append("retrieve")
            if self.last_action!=0:
                q_t = self.construct_query()
                self.d_t, zt = self.ret.retrieve(q_t, k=1, num_search=4)
                self.d_t = self.d_t.squeeze(1)
                self.hat_y_t = self.hat_y_t  # Keep current response
        elif action == 1:  # Proceed Response
            self.action_history.append("proceed")
            if self.n+1 < len(self.y):
                self.n += 1
                self.hat_y_t, self.log_prob = self.get_next_response()
                self.response_cache.append(self.hat_y_t[0])
            else:
                self.done=True
        elif action == 2:  # Rewrite Current Response
            self.action_history.append("rewrite")
            if self.n>-1:
                self.response_cache.pop()
                self.hat_y_t, self.log_prob = self.get_next_response()
                self.response_cache.append(self.hat_y_t[0])
            else:
                reward+=-1
        elif action == 3:  # Output Answer
            self.done = True
        # if self.hat_y_t!=None and self.eos_id in self.hat_y_t[0]:
        #     self.done=True
        if len(self.action_history)>self.history_len:
            self.action_history.pop(0)
        if self.steps>3*len(self.y)+5:
            self.done=True
        reward = reward + self.compute_reward()
        next_state = self.get_state()
        self.steps += 1
        self.last_action=self.action
        return next_state, reward, self.done, {}

    def compute_reward(self):
        # Implement reward calculation logic
        reward=0
        if self.done:
            if self.n>-1:
                reward += 2*(Bert_score([self.cat_response(self.response_cache)], [" ".join(self.y)])[0] - self.basic_reward)
                reward += 0.1*((self.n+1)/len(self.y))**2
                reward += sum(self.halulu)
        # elif self.action == 0:
        #     if self.last_action!=0:
        #         reward += Bert_score([self.collate.datatokenizer.decode(self.d_t[0])], [" ".join(self.y)])[0]/len(self.y)
        if self.action==1:
            reward += Bert_score([self.cat_response(self.response_cache[-1:])], [self.y[self.n]])[0]/len(self.y)
            self.halulu.append(0.5*self.log_prob[0].exp().mean()/len(self.y))
        elif self.action==2:
            reward -= 0.01
            if self.n>-1:
                reward += Bert_score([self.cat_response(self.response_cache[-1:])], [self.y[self.n]])[0]/len(self.y)
                self.halulu.pop(-1)
                self.halulu.append(0.5*self.log_prob[0].exp().mean()/len(self.y))
                for i in reversed(range(self.steps)):
                    if self.revise_reward[i]>0:
                        self.revise_reward[i]=0
                        break
            else:
                reward+=-0.05
        reward = float(reward)
        self.revise_reward.append(reward)
        return reward
    
    def construct_query(self):
        self.x, self.d_t, self.response_cache
        # Implement query construction logic
        return self.x + self.cat_response(self.response_cache[-self.history_len:])

    def cat_response(self, cache:list[Tensor])->str:
        if cache[0]==None:
            cache = cache[1:]
        if len(cache)==0:
            return ""
        
        s = self.LM.tokenizer.decode(torch.cat(cache))
        return s
    def get_next_response(self,):
        # Implement response generation logic
        # messages, answer = self.collate.templete(self.x, ' '.join(self.response_cache))
        messages, answer = self.collate.templete(self.x, self.cat_response(self.response_cache))
        if self.d_t!=None:
            d_t = tensor_retuen_type(input_ids = self.d_t, attention_mask = torch.ones_like(self.d_t)).to(self.LM.device)
        # print("What is feeded:",messages+" "+answer, self.y[self.n])
        response, log_prob = self.LM.pseudo_generate(messages+" "+answer, self.y[self.n], Doc_tokens = d_t, temperture = 0.5, return_prob = True, decode = False)
        
        return response, log_prob
    def get_basic_response(self,x, y):
        # Implement response generation logic
        # messages, answer = self.collate.templete(self.x, ' '.join(self.response_cache))
        messages, answer = self.collate.templete(x, "")
        if self.d_t!=None:
            d_t = tensor_retuen_type(input_ids = self.d_t, attention_mask = torch.ones_like(self.d_t)).to(self.LM.device)
        # print("What is feeded:",messages+" "+answer, self.y[self.n])
        response = self.LM.pseudo_generate(messages+" "+answer, y, Doc_tokens = d_t, temperture = 0.5, return_prob = False, decode = True)
        
        return response

if __name__=='__main__':
    
    
    # Example usage
    env = LLMEnv()
    model = BertAgentCritic(agent_size_config, env.action_space_size)
    trainer = PPOTrainer(model)

    # Training loop
    for episode in range(1000):
        state = env.reset()  # Shape: string
        done = False
        memory = []

        while not done:
            token_logits, action_logits, state_value = model([state])  # action_logits shape: (1, action_space_size), state_value shape: (1, 1)
            token_dist = Categorical(logits = token_logits)
            action_dist = Categorical(logits = action_logits)
            tokens = token_dist.sample()
            action = action_dist.sample()  # Shape: (1,)
            print(model.tokenizer.batch_decode(tokens))
            exit()

            next_state, reward, done, _ = env.step(action.item())  # next_state shape: string, reward shape: scalar, done shape: scalar (boolean)
            memory.append((state, action, dist.log_prob(action), reward, done, state_value))  # Shapes: (string, (1,), (1, action_space_size), scalar, scalar (boolean), (1, 1))

            state = next_state

        trainer.update(memory)