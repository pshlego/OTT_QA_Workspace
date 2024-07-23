import os
import re
import sys
import ast
import copy
import json
import time
import vllm
import hydra
import torch
import unicodedata
from tqdm import tqdm
from pymongo import MongoClient
from omegaconf import DictConfig
from transformers import set_seed
from ColBERT.colbert import Searcher
from ColBERT.colbert.infra import ColBERTConfig
from FlagEmbedding import LayerWiseFlagLLMReranker
from Ours.dpr.data.qa_validation import has_answer
from Ours.dpr.utils.tokenizers import SimpleTokenizer
from prompts import select_table_segment_prompt, select_passage_prompt
# VLLM Parameters
COK_VLLM_TENSOR_PARALLEL_SIZE = 4 # TUNE THIS VARIABLE depending on the number of GPUs you are requesting and the size of your model.
COK_VLLM_GPU_MEMORY_UTILIZATION = 0.6 # TUNE THIS VARIABLE depending on the number of GPUs you are requesting and the size of your model.
set_seed(0)


@hydra.main(config_path = "conf", config_name = "graph_query_algorithm")
def main(cfg: DictConfig):
    # load qa dataset
    print()
    print(f"[[ Loading qa dataset... ]]", end = "\n\n")
    qa_dataset = json.load(open(cfg.qa_dataset_path))
    graph_query_engine = GraphQueryEngine(cfg)
    tokenizer = SimpleTokenizer()
    
    # query
    print(f"Start querying...")
    recall_list = []
    error_cases = []
    query_time_list = []
    retrieved_query_list = []
    for qidx, qa_datum in tqdm(enumerate(qa_dataset), total=len(qa_dataset)):
        
        nl_question = qa_datum['question']
        answers = qa_datum['answers']
        
        if qidx < 255:
            continue
        
        init_time = time.time()
        retrieved_graph = graph_query_engine.query(nl_question, retrieval_time = 2)
        end_time = time.time()
        query_time_list.append(end_time - init_time)
        
        retrieved_query_list.append(retrieved_graph)
        
        to_print = {
            "qa data": qa_datum,
            "retrieved graph": retrieved_graph
        }
        with open(cfg.final_result_path, 'a') as file:
            file.write(json.dumps(to_print) + '\n')
    
    # save integrated graph
    # print(f"Saving integrated graph...")
    # json.dump(retrieved_query_list, open(cfg.integrated_graph_save_path, 'w'))
    # json.dump(query_time_list, open(cfg.query_time_save_path, 'w'))
    # json.dump(error_cases, open(cfg.error_cases_save_path, 'w'))




class GraphQueryEngine:
    
    def __init__(self, cfg):

        # 1. Load data graph 1:
            # Edge id to node pair
        print(f"1. Loading edge contents...")
        edge_contents = []
        EDGES_NUM = 17151500
        with open(cfg.edge_dataset_path, "r") as file:
            for line in tqdm(file, total = EDGES_NUM):
                edge_contents.append(json.loads(line))
        num_of_edges = len(edge_contents)
        print("1. Loaded " + str(num_of_edges) + " edges!")
        self.edge_key_to_content = {}
        self.table_key_to_edge_keys = {}
        print("1. Processing edges...")
        for id, edge_content in tqdm(enumerate(edge_contents), total = len(edge_contents)):
            self.edge_key_to_content[edge_content['chunk_id']] = edge_content
            
            if str(edge_content['table_id']) not in self.table_key_to_edge_keys:
                self.table_key_to_edge_keys[str(edge_content['table_id'])] = []
            
            self.table_key_to_edge_keys[str(edge_content['table_id'])].append(id) 
        print("1. Processing edges complete", end = "\n\n")
        
        # 2. Load data graph 2:
            # Table id to linked passages
        SIZE_OF_DATA_GRAPH = 839810
        print("2. Loading data graph...")
        entity_linking_results = []
        with open(cfg.entity_linking_dataset_path, "r") as file:
            for line in tqdm(file, total = SIZE_OF_DATA_GRAPH):
                entity_linking_results.append(json.loads(line))
        num_of_entity_linking_results = len(entity_linking_results)
        print("2. Loaded " + str(num_of_entity_linking_results) + " tables!")
        self.table_chunk_id_to_linked_passages = {}
        print("2. Processing data graph...")
        for entity_linking_content in tqdm(entity_linking_results):
            self.table_chunk_id_to_linked_passages[entity_linking_content['table_chunk_id']] = entity_linking_content
        print("2. Processing data graph complete!", end = "\n\n")
        
        # 3. Load tables
        TOTAL_NUM_OF_TABLES = 840895
        print("3. Loading tables...")
        self.table_key_to_content = {}
        self.table_title_to_table_key = {}
        table_contents = json.load(open(cfg.table_data_path))
        print("3. Loaded " + str(len(table_contents)) + " tables!")
        print("3. Processing tables...")
        for table_key, table_content in tqdm(enumerate(table_contents)):
            self.table_key_to_content[str(table_key)] = table_content
            self.table_title_to_table_key[table_content['chunk_id']] = table_key
        print("3. Processing tables complete!", end = "\n\n")
        
        # 4. Load passages
        print("4. Loading passages...")
        self.passage_key_to_content = {}
        passage_contents = json.load(open(cfg.passage_data_path))
        print("4. Loaded " + str(len(passage_contents)) + " passages!")
        print("4. Processing passages...")
        for passage_content in tqdm(passage_contents):
            self.passage_key_to_content[passage_content['title']] = passage_content
        print("4. Processing passages complete!", end = "\n\n")

        # 5. Load Retrievers
            ## id mappings
        print("5. Loading retrievers...")
        self.id_to_edge_key = json.load(open(cfg.edge_ids_path))
        self.id_to_table_key = json.load(open(cfg.table_ids_path))
        self.id_to_passage_key = json.load(open(cfg.passage_ids_path))
        print("5. Loaded retrievers complete!", end = "\n\n")
        
        # 6. Load ColBERT retrievers
        print("6. Loading index...")
        print("6 (1/4). Loading ColBERT edge retriever...")
        disablePrint()
        self.colbert_edge_retriever = Searcher(index=f"{cfg.edge_index_name}.nbits{cfg.nbits}", config=ColBERTConfig(), collection=cfg.collection_edge_path, index_root=cfg.edge_index_root_path, checkpoint=cfg.edge_checkpoint_path)
        enablePrint()
        print("6 (1/4). Loaded index complete!")
        print("6 (2/4). Loading ColBERT table retriever...")
        disablePrint()
        self.colbert_table_retriever = Searcher(index=f"{cfg.table_index_name}.nbits{cfg.nbits}", config=ColBERTConfig(), collection=cfg.collection_table_path, index_root=cfg.table_index_root_path, checkpoint=cfg.table_checkpoint_path)
        enablePrint()
        print("6 (2/4). Loaded ColBERT table retriever complete!")
        print("6 (3/4). Loading ColBERT passage retriever...")
        disablePrint()
        self.colbert_passage_retriever = Searcher(index=f"{cfg.passage_index_name}.nbits{cfg.nbits}", config=ColBERTConfig(), collection=cfg.collection_passage_path, index_root=cfg.passage_index_root_path, checkpoint=cfg.passage_checkpoint_path)
        enablePrint()
        print("6 (3/4). Loaded ColBERT passage retriever complete!")
        print("6 (4/4). Loading reranker...")
        self.cross_encoder_edge_retriever = LayerWiseFlagLLMReranker(cfg.reranker_checkpoint_path, use_fp16=True)
        print("6 (4/4). Loading reranker complete!")
        print("6. Loaded index complete!", end = "\n\n")
        
        # 7. Load LLM
        # print("7. Loading large language model...")
        # self.llm = vllm.LLM(
        #     cfg.llm_path,
        #     worker_use_ray = True,
        #     tensor_parallel_size = COK_VLLM_TENSOR_PARALLEL_SIZE, 
        #     gpu_memory_utilization = COK_VLLM_GPU_MEMORY_UTILIZATION, 
        #     trust_remote_code = True,
        #     dtype = "half", # note: bfloat16 is not supported on nvidia-T4 GPUs
        #     max_model_len = 6144, # input length + output length
        #     enforce_eager = True,
        # )
        # print("7. Loaded large language model complete!", end = "\n\n")
        
        # 8. Load tokenizer
        # print("8. Loading tokenizer...")
        # self.tokenizer = self.llm.get_tokenizer()
        # print("8. Loaded tokenizer complete!", end = "\n\n")
        
        ## prompt templates
        self.select_table_segment_prompt = select_table_segment_prompt
        self.select_passage_prompt = select_passage_prompt
        
        # load experimental settings
        self.top_k_of_edge = cfg.top_k_of_edge
        self.top_k_of_table_segment_augmentation = cfg.top_k_of_table_segment_augmentation
        self.top_k_of_passage_augmentation = cfg.top_k_of_passage_augmentation
        self.top_k_of_table_segment = cfg.top_k_of_table_segment
        self.top_k_of_passage = cfg.top_k_of_passage
        self.top_k_of_table_select_w_llm = cfg.top_k_of_table_select_w_llm

        self.node_scoring_method = cfg.node_scoring_method
        self.batch_size = cfg.batch_size
        
        self.mid_result_path = cfg.mid_result_path
















    #############################################################################
    # Query                                                                     #
    # Input: NL Question                                                        #
    # Input: retrieval_time (Number of iterations)                              #
    # Output: Retrieved Graphs (Subgraph of data graph relevant to the NL       #
    #                           question)                                       #  
    # ------------------------------------------------------------------------- # 
    #############################################################################
    def query(self, nl_question, retrieval_time = 2):
        
        # 1. Edge Retrieval
        retrieved_edges = self.retrieve_edges(nl_question)
        
        # 2. Graph Integration
        integrated_graph = self.integrate_graphs(retrieved_edges)
        retrieval_type = None
        
        for i in range(retrieval_time):
            
            # From second iteration
            if i >= 1:
                self.reranking_edges(nl_question, integrated_graph)
                retrieval_type = 'edge_reranking'
                self.assign_scores(integrated_graph, retrieval_type)
            
            # Is not last iteration
            if i < retrieval_time:
                topk_table_segment_nodes = []
                topk_passage_nodes = []
                for node_id, node_info in integrated_graph.items():
                    if node_info['type'] == 'table segment':
                        topk_table_segment_nodes.append([node_id, node_info['score']])
                    elif node_info['type'] == 'passage':
                        topk_passage_nodes.append([node_id, node_info['score']])

                    # Default: 10
                topk_table_segment_nodes = sorted(topk_table_segment_nodes, key=lambda x: x[1], reverse=True)[:self.top_k_of_table_segment_augmentation]
                    # Default: 0
                topk_passage_nodes = sorted(topk_passage_nodes, key=lambda x: x[1], reverse=True)[:self.top_k_of_passage_augmentation]
                
                
                # Expanded Query Retrieval
                    # 3.1 Passage Node Augmentation
                self.augment_node(integrated_graph, nl_question, topk_table_segment_nodes, 'table segment', 'passage', i)
                    # 3.2 Table Segment Node Augmentation
                self.augment_node(integrated_graph, nl_question, topk_passage_nodes, 'passage', 'table segment', i)
                
                self.assign_scores(integrated_graph, retrieval_type)

            # Is last iteration
            if i == retrieval_time - 1:
                table_key_list, deleted_node_id_list, table_id_to_linked_nodes = self.get_table(integrated_graph)
                
                table_id_to_row_id_to_linked_passage_ids, table_id_to_table_info \
                                                    = self.combine_linked_passages(table_id_to_linked_nodes)
                
                
                selected_table_segment_list = self.select_table_segments(
                                                            nl_question, 
                                                            table_id_to_row_id_to_linked_passage_ids,
                                                            table_id_to_table_info
                                                        )
                self.select_passages(nl_question, selected_table_segment_list, integrated_graph)
            
            self.assign_scores(integrated_graph, retrieval_type)
            retrieved_graphs = integrated_graph
        
        return retrieved_graphs
    
    
    
    
    
    
    
    
    
    
    
    
    #############################################################################
    # RetrieveEdges                                                             #
    # Input: NL Question                                                        #
    # Output: Retrieved Edges                                                   #
    # ------------------------------------------------------------------------- #
    # Retrieve `top_k_of_edge` number of edges (table segment - passage)        #
    # relevant to the input NL question.                                        #
    #############################################################################
    def retrieve_edges(self, nl_question):
        
        retrieved_edges_info = self.colbert_edge_retriever.search(nl_question, 10000)
        
        retrieved_edge_id_list = retrieved_edges_info[0]
        retrieved_edge_score_list = retrieved_edges_info[2]
        
        retrieved_edge_contents = []
        for graphidx, retrieved_id in enumerate(retrieved_edge_id_list[:self.top_k_of_edge]):
            retrieved_edge_content = self.edge_key_to_content[self.id_to_edge_key[str(retrieved_id)]]

            # pass single node graph
            if 'linked_entity_id' not in retrieved_edge_content:
                continue
            
            retrieved_edge_content['edge_score'] = retrieved_edge_score_list[graphidx]
            retrieved_edge_contents.append(retrieved_edge_content)

        return retrieved_edge_contents

    #############################################################################
    # IntegrateGraphs                                                           #
    # Input: retrieved_graphs                                                   #
    # Output: integrated_graph with scores assigned to each node                #
    # ------------------------------------------------------------------------- #
    # Integrate `retrieved_graphs`(list of edges relevant to the NL question)   #
    # into `self.graph`, a dictionary containing the information about the      #
    # nodes, via addNode function.                                              #
    # The nodes are assigned scores via `assign_scores` function.               #
    #############################################################################
    def integrate_graphs(self, retrieved_graphs):
        integrated_graph = {}
        retrieval_type = 'edge_retrieval'
        # graph integration
        for retrieved_graph_content in retrieved_graphs:
            graph_chunk_id = retrieved_graph_content['chunk_id']

            # get table segment node info
            table_key = str(retrieved_graph_content['table_id'])
            row_id = graph_chunk_id.split('_')[1]
            table_segment_node_id = f"{table_key}_{row_id}"
            
            # get passage node info
            passage_id = retrieved_graph_content['linked_entity_id']
            
            # get two node graph score
            edge_score = retrieved_graph_content['edge_score']
            
            # add nodes
            self.add_node(integrated_graph, 'table segment', table_segment_node_id, passage_id, edge_score, retrieval_type)
            self.add_node(integrated_graph, 'passage', passage_id, table_segment_node_id, edge_score, retrieval_type)

            # node scoring
            self.assign_scores(integrated_graph)

        return integrated_graph



    #############################################################################
    # RerankingEdges                                                            #
    # Input: NL Question                                                        #
    # InOut: Retrieved Graphs - actually just any `graph`                       #
    # ------------------------------------------------------------------------- #
    # Recalculate scores for edges in the `retrieved_graphs` using the cross    #
    # encoder edge reranker.                                                    #
    # The scores are assigned via `addNode` function.                           #
    #############################################################################
    # TODO: change after on (add_node -> change score)
    def reranking_edges(self, nl_question, retrieved_graphs):
        edges = []
        edges_set = set()
        
        for node_id, node_info in retrieved_graphs.items():
            
            if node_info['type'] == 'table segment':
                table_id = node_id.split('_')[0]
                row_id = int(node_id.split('_')[1])
                table = self.table_key_to_content[table_id]
                table_title = table['title']
                table_rows = table['text'].split('\n')
                column_names = table_rows[0]
                row_values = table_rows[row_id+1]
                table_text = table_title + ' [SEP] ' + column_names + ' [SEP] ' + row_values

                for linked_node in node_info['linked_nodes']:
                    
                    linked_node_id = linked_node[0]
                    edge_id = f"{node_id}_{linked_node_id}"
                    
                    if edge_id not in edges_set:
                        edges_set.add(edge_id)
                    else:
                        continue
                    
                    passage_text = self.passage_key_to_content[linked_node_id]['text']
                    graph_text = table_text + ' [SEP] ' + passage_text
                    edges.append({'table_segment_node_id': node_id, 'passage_id': linked_node_id, 'text': graph_text})

        for i in range(0, len(edges), self.batch_size):
            edge_batch = edges[i : i + self.batch_size]
            model_input = [[nl_question, edge['text']] for edge in edge_batch]
            with torch.no_grad():
                edge_scores = self.cross_encoder_edge_retriever.compute_score(model_input, batch_size = 200, cutoff_layers = [40], max_length = 256)
            
                for edge, score in zip(edge_batch, edge_scores):
                    table_segment_node_id = edge['table_segment_node_id']
                    passage_id = edge['passage_id']
                    reranking_score = float(score)            
                    self.add_node(retrieved_graphs, 'table segment', table_segment_node_id, passage_id, reranking_score, 'edge_reranking')
                    self.add_node(retrieved_graphs, 'passage', passage_id, table_segment_node_id, reranking_score, 'edge_reranking')
    
    
    
    #############################################################################
    # AssignScores                                                              #
    # InOut: graph                                                              #
    # Input: retrieval_type (default = None)                                    #
    # ------------------------------------------------------------------------- #
    # Assign scores to the nodes in the graph using weights of the edges        #
    # connected to each node                                                    #
    #  - min: minimum score of the linked nodes                                 #
    #  - max: maximum score of the linked nodes                                 #
    #  - mean: mean score of the linked nodes                                   #
    # The score is assigned in graph[node_id]["score"]                          #
    #############################################################################
    def assign_scores(self, graph, retrieval_type = None):
        for node_id, node_info in graph.items():
            if retrieval_type is not None:
                filtered_retrieval_type = ['edge_retrieval', 'passage_node_augmentation_0', 'table_segment_node_augmentation_0']
                linked_scores = [linked_node[1] for linked_node in node_info['linked_nodes'] if linked_node[2] not in filtered_retrieval_type]
            else:
                linked_scores = [linked_node[1] for linked_node in node_info['linked_nodes']]
            
            if self.node_scoring_method == 'min':
                node_score = min(linked_scores)
            elif self.node_scoring_method == 'max':
                node_score = max(linked_scores)
            elif self.node_scoring_method == 'mean':
                node_score = sum(linked_scores) / len(linked_scores)
            
            graph[node_id]['score'] = node_score
    
    
    
    
    #############################################################################
    # AugmentNode                                                               #
    # InOut: graph                                                              #
    # Input: nl_question                                                        #
    # Input: topk_query_nodes (`k` nodes which show the highest scores)         #
    # Input: query_node_type                                                    #
    # Input: retrieved_node_type                                                #
    # Input: retrieval_time                                                     #
    # ------------------------------------------------------------------------- #
    # Add 20 edges for target nodes which are the top k nodes in the query      #
    #############################################################################
    def augment_node(self, graph, nl_question, topk_query_nodes, query_node_type, retrieved_node_type, retrieval_time):
        
        for source_rank, (query_node_id, query_node_score) in enumerate(topk_query_nodes):
            
            expanded_query = self.get_expanded_query(nl_question, query_node_id, query_node_type)
            
            if query_node_type == 'table segment':
                retrieved_node_info = self.colbert_passage_retriever.search(expanded_query, 20)
                retrieved_id_list = retrieved_node_info[0]
                # retrieved_score_list = retrieved_node_info[2]
                top_k = self.top_k_of_passage
            else:
                retrieved_node_info = self.colbert_table_retriever.search(expanded_query, 20)
                retrieved_id_list = retrieved_node_info[0]
                # retrieved_score_list = retrieved_node_info[2]
                top_k = self.top_k_of_table_segment

            for target_rank, retrieved_id in enumerate(retrieved_id_list):
                if target_rank >= top_k:
                    break
                
                if query_node_type == 'table segment':
                    retrieved_node_id = self.id_to_passage_key[str(retrieved_id)]
                    augment_type = f'passage_node_augmentation_{retrieval_time}'
                else:
                    retrieved_node_id = self.id_to_table_key[str(retrieved_id)]
                    augment_type = f'table_segment_node_augmentation_{retrieval_time}'
                
                self.add_node(graph, query_node_type, query_node_id, retrieved_node_id, query_node_score, augment_type, source_rank, target_rank)
                self.add_node(graph, retrieved_node_type, retrieved_node_id, query_node_id, query_node_score, augment_type, target_rank, source_rank)
    
    
    #############################################################################
    # getExpandedQuery                                                          #
    # Input: nl_question                                                        #
    # Input: node_id                                                            #
    # Input: query_node_type                                                    #
    # Output: expanded_query                                                    #
    # ------------------------------------------------------------------------- #
    # generate an expanded query (string to query) using the information of the #
    # node (either can be table segment or passage).                            #
    #############################################################################
    def get_expanded_query(self, nl_question, node_id, query_node_type):
        if query_node_type == 'table segment':
            table_key = node_id.split('_')[0]
            table = self.table_key_to_content[table_key]
            table_title = table['title']
            
            row_id = int(node_id.split('_')[1])
            table_rows = table['text'].split('\n')
            column_name = table_rows[0]
            row_values = table_rows[row_id+1]
            
            expanded_query = f"{nl_question} [SEP] {table_title} [SEP] {column_name} [SEP] {row_values}"
        else:
            passage = self.passage_key_to_content[node_id]
            passage_title = passage['title']
            passage_text = passage['text']
            
            expanded_query = f"{nl_question} [SEP] {passage_title} [SEP] {passage_text}"
        
        return expanded_query
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    #############################################################################
    # getTable                                                                  #
    # Input: retrieved_graph                                                    #
    # Output: table_key_list                                                    #
    # Output: node_id_list                                                      #
    # Output: table_key_to_augmented_nodes                                      #
    # ------------------------------------------------------------------------- #
    # Using the retrieved_graph, get the tables according to the scores of the  #
    # table segment-typed nodes.                                                #
    # Sorted in the reveresed order of the scores, aggregate the original table #
    # node of what were the table segment nodes.                                #
    #############################################################################
    def get_table(self, retrieved_graph):
        sorted_retrieved_graph = sorted(retrieved_graph.items(), key=lambda x: x[1]['score'], reverse=True)
        
        table_key_list = []
        node_id_list = []
        table_key_to_augmented_nodes = {}
        for node_id, node_info in sorted_retrieved_graph:
            if node_info['type'] == 'table segment' and len(table_key_list) < self.top_k_of_table_select_w_llm:
                table_key = node_id.split('_')[0]
                if table_key not in table_key_list:
                    table_key_list.append(table_key)
                    table_key_to_augmented_nodes[table_key] = {}
                
                # table_key_to_augmented_nodes[table_key][node_id.split('_')[-1]] = [node_info[0] for node_info in node_info['linked_nodes'] if 'augmentation' in node_info[2]]
                table_key_to_augmented_nodes[table_key][node_id.split('_')[-1]] = list(set([node_info[0] for node_info in node_info['linked_nodes']]))
                
            if node_info['type'] == 'table segment' and node_id.split('_')[0] in table_key_list:
                node_id_list.append(node_id)

        return table_key_list, node_id_list, table_key_to_augmented_nodes
    

    #############################################################################
    # combineLinkedPassages                                                     #
    # Input: table_id_to_linked_nodes                                           #
    # Output: table_id_to_row_id_to_linked_passage_ids                          #
    # Output: table_id_to_table_info                                            #
    # ------------------------------------------------------------------------- #
    # 
    #############################################################################
    def combine_linked_passages(self, table_id_to_linked_nodes):

        table_id_to_row_id_to_linked_passage_ids = {}
        table_id_to_table_info = {}
        
        for table_id, table_segment_to_linked_nodes in table_id_to_linked_nodes.items():
            if table_id not in table_id_to_row_id_to_linked_passage_ids:
                table_id_to_row_id_to_linked_passage_ids[table_id] = {}
            
            
            # 1. Calculate table info and put it in `table_id_to_table_info`
            linked_passage_info = self.table_chunk_id_to_linked_passages[self.table_key_to_content[table_id]['chunk_id']]
            table_title = linked_passage_info['question'].split(' [SEP] ')[0]
            table_column_name = linked_passage_info['question'].split(' [SEP] ')[-1].split('\n')[0]
            table_rows = linked_passage_info['question'].split(' [SEP] ')[-1].split('\n')[1:]
            table_rows = [row for row in table_rows if row != ""]
            table_info = {"title": table_title, "column_name": table_column_name, "rows": table_rows}
            
            table_id_to_table_info[table_id] = table_info
            
            
            # row_id_to_linked_passage_contents = {}
            # for linked_passage_info in linked_passages:
            #     row_id = str(linked_passage_info['row'])
            #     if row_id not in row_id_to_linked_passage_contents:
            #         row_id_to_linked_passage_contents[row_id] = []
            #
            #     if row_id not in table_id_to_linked_passage_ids[table_key]:
            #         table_id_to_linked_passage_ids[table_key][row_id] = []
            #
            #     try:
            #         row_id_to_linked_passage_contents[row_id].append(self.passage_key_to_content[linked_passage_info['retrieved'][0]]['text'])
            #         table_id_to_linked_passage_ids[table_key][row_id].append([linked_passage_info['retrieved'][0], 'entity_linking'])
            #     except:
            #         continue
            
            
            # 2. Calculate table row to table linked passages and put it in
                #       `table_id_to_row_id_to_linked_passage_ids`
            linked_passages = linked_passage_info['results']
            for linked_passage_info in linked_passages:
                row_id = str(linked_passage_info['row'])
                
                if row_id not in table_id_to_row_id_to_linked_passage_ids[table_id]:
                    table_id_to_row_id_to_linked_passage_ids[table_id][row_id] = []
            
            for row_id, linked_nodes in table_segment_to_linked_nodes.items():
                
                if row_id not in table_id_to_row_id_to_linked_passage_ids[table_id]:
                    table_id_to_row_id_to_linked_passage_ids[table_id][row_id] = []
                
                for node in list(set(linked_nodes)):
                    try:    table_id_to_row_id_to_linked_passage_ids[table_id][row_id].append(node)
                    except: continue
            
        return table_id_to_row_id_to_linked_passage_ids
    
    
    
    #############################################################################
    # selectTableSegments                                                       #
    # Input: nl_question                                                        #
    # Input: table_id_to_row_id_to_linked_passage_ids                           #
    # Input: table_id_to_table_info                                             #
    # Output: selected_table_segment_list                                       #
    # ------------------------------------------------------------------------- #
    # For each table, examine its entire table info and linked passages to pick #
    # the most relevant table segments, using the llm.                          #
    #############################################################################
    def select_table_segments(self, nl_question, table_id_to_row_id_to_linked_passage_ids, table_id_to_table_info):
        
        prompt_list = []
        table_id_list = []
        table_id_to_linked_passage_ids = {}
        
        for table_id in table_id_to_table_info.keys():
                    
            ######################################################################################################
            
            table_and_linked_passages = self.stringify_table_and_linked_passages(
                                                table_id_to_table_info[table_id],
                                                table_id_to_row_id_to_linked_passage_ids[table_id]
                                            )
            contents_for_prompt = {'question': nl_question, 'table_and_linked_passages': table_and_linked_passages}
            prompt = self.get_prompt(contents_for_prompt)
            prompt_list.append(prompt)
            table_id_list.append(table_id)
            
        responses = self.llm.generate(
                                    prompt_list,
                                    vllm.SamplingParams(
                                        n = 1,  # Number of output sequences to return for each prompt.
                                        top_p = 0.9,  # Float that controls the cumulative probability of the top tokens to consider.
                                        temperature = 0.5,  # randomness of the sampling
                                        skip_special_tokens = True,  # Whether to skip special tokens in the output.
                                        max_tokens = 64,  # Maximum number of tokens to generate per output sequence.
                                        logprobs = 1
                                    ),
                                    use_tqdm = False
                                )
        
        selected_table_segment_list = []
        for table_id, response in zip(table_id_list, responses):
            selected_rows = response.outputs[0].text
            try:
                selected_rows = ast.literal_eval(selected_rows)
                selected_rows = [string.strip() for string in selected_rows]
            except:
                selected_rows = [selected_rows.strip()]
                
            for selected_row in selected_rows:
                try:
                    row_id = selected_row.split('_')[1]
                    row_id = str(int(row_id) - 1)
                    _ = table_id_to_linked_passage_ids[table_id][str(row_id)]
                except:
                    continue
                
                selected_table_segment_list.append(
                    {
                        "table_segment_node_id": f"{table_id}_{row_id}", 
                        "linked_passages": table_id_to_linked_passage_ids[table_id][str(row_id)]
                    }
                )
            
        return selected_table_segment_list
    
    
    #############################################################################
    # stringifyTableAndLinkedPassages                                           #
    # Input: table_info                                                         #
    # Input: row_id_to_linked_passage_ids                                       #
    # Output: table_and_linked_passages (string)                                #
    # ------------------------------------------------------------------------- #
    # Make the table and its linked passages in a string format.                #
    #############################################################################
    def stringify_table_and_linked_passages(self, table_info, row_id_to_linked_passage_ids):
        # 1. Stringify table metadata
        table_and_linked_passages = ""
        table_and_linked_passages += f"Table Name: {table_info['title']}\n"
        table_and_linked_passages += f"Column Name: {table_info['column_name']\
                                        .replace(' , ', '[SPECIAL]')\
                                        .replace(', ', ' | ')\
                                        .replace('[SPECIAL]', ' , ')}\n\n"
        
        # 2. Stringify each row + its linked passages
        for row_id, row_content in enumerate(table_info['rows']):
            table_and_linked_passages += f"Row_{row_id + 1}: {row_content\
                                                                .replace(' , ', '[SPECIAL]')\
                                                                .replace(', ', ' | ')\
                                                                .replace('[SPECIAL]', ' , ')}\n"
                                                                
            if str(row_id) in row_id_to_linked_passage_ids:
                table_and_linked_passages += f"Passages linked to Row_{row_id + 1}:\n"
                
                for linked_passage_id in list(set(row_id_to_linked_passage_ids[str(row_id)])):
                    
                    linked_passage_content = self.passage_key_to_content[linked_passage_id]['text']
                    
                    tokenized_content = self.tokenizer.encode(linked_passage_content)
                    trimmed_tokenized_content = tokenized_content[:96]
                    trimmed_content = self.tokenizer.decode(trimmed_tokenized_content)
                    table_and_linked_passages += f"- {trimmed_content}\n"
                    
            table_and_linked_passages += "\n\n"

        return table_and_linked_passages
    
    
    
    #############################################################################
    # SelectTableSegments                                                       #
    # Input: NL Question                                                        #
    # InOut: table_key_to_augmented_nodes                                       #
    # ------------------------------------------------------------------------- #
    # Rip each table into table segments and its linked passage nodes.          #
    # For each table segment, put its content and its linked passages into the  #
    #   prompt, and get the response from the LLM.                              #
    # Parse the response, then select the table segments and its linked         #
    #   passages into a dictionary (a list of them is returned).                #
    #############################################################################
    # def select_table_old_segments(self, nl_question, table_key_to_augmented_nodes):
    #     prompt_list = []
    #     table_key_list = []
    #     table_id_to_linked_passage_ids = {}
    #     for table_key, table_segment_to_augmented_nodes in table_key_to_augmented_nodes.items():
    #         if table_key not in table_id_to_linked_passage_ids:
    #             table_id_to_linked_passage_ids[table_key] = {}
    #
    #         linked_passage_info = self.table_chunk_id_to_linked_passages[self.table_key_to_content[table_key]['chunk_id']]
    #         table_title = linked_passage_info['question'].split(' [SEP] ')[0]
    #         table_column_name = linked_passage_info['question'].split(' [SEP] ')[-1].split('\n')[0]
    #         table_rows = linked_passage_info['question'].split(' [SEP] ')[-1].split('\n')[1:]
    #         table_rows = [row for row in table_rows if row != ""]
    #         table_info = {"title": table_title, "column_name": table_column_name, "rows": table_rows}
    #         linked_passages = linked_passage_info['results']
    #
    #         row_id_to_linked_passage_contents = {}
    #         for linked_passage_info in linked_passages:
    #             row_id = str(linked_passage_info['row'])
    #             if row_id not in row_id_to_linked_passage_contents:
    #                 row_id_to_linked_passage_contents[row_id] = []
    #
    #             if row_id not in table_id_to_linked_passage_ids[table_key]:
    #                 table_id_to_linked_passage_ids[table_key][row_id] = []
    #
    #             try:
    #                 row_id_to_linked_passage_contents[row_id].append(self.passage_key_to_content[linked_passage_info['retrieved'][0]]['text'])
    #                 table_id_to_linked_passage_ids[table_key][row_id].append([linked_passage_info['retrieved'][0], 'entity_linking'])
    #             except:
    #                 continue
    #
    #         for row_id, augmented_nodes in table_segment_to_augmented_nodes.items():
    #             if row_id not in row_id_to_linked_passage_contents:
    #                 row_id_to_linked_passage_contents[row_id] = []
    #
    #             if row_id not in table_id_to_linked_passage_ids[table_key]:
    #                 table_id_to_linked_passage_ids[table_key][row_id] = []
    #
    #             for augmented_node in list(set(augmented_nodes)):
    #                 try:
    #                     row_id_to_linked_passage_contents[row_id].append(self.passage_key_to_content[augmented_node]['text'])
    #                     table_id_to_linked_passage_ids[table_key][row_id].append([augmented_node, 'passage_node_augmentation'])
    #                 except:
    #                     continue
    #
    #         ######################################################################################################
    #
    #         table_and_linked_passages = self.get_table_and_linked_passages(table_info, row_id_to_linked_passage_contents)
    #         contents_for_prompt = {'question': nl_question, 'table_and_linked_passages': table_and_linked_passages}
    #         prompt = self.get_prompt(contents_for_prompt)
    #         prompt_list.append(prompt)
    #         table_key_list.append(table_key)
    #
    #     responses = self.llm.generate(
    #             prompt_list,
    #             vllm.SamplingParams(
    #                 n = 1,  # Number of output sequences to return for each prompt.
    #                 top_p = 0.9,  # Float that controls the cumulative probability of the top tokens to consider.
    #                 temperature = 0.5,  # randomness of the sampling
    #                 skip_special_tokens = True,  # Whether to skip special tokens in the output.
    #                 max_tokens = 64,  # Maximum number of tokens to generate per output sequence.
    #                 logprobs = 1
    #             ),
    #             use_tqdm = False
    #         )
    #
    #     selected_table_segment_list = []
    #     for table_key, response in zip(table_key_list, responses):
    #         selected_rows = response.outputs[0].text
    #         try:
    #             selected_rows = ast.literal_eval(selected_rows)
    #             selected_rows = [string.strip() for string in selected_rows]
    #         except:
    #             selected_rows = [selected_rows.strip()]
    #
    #         for selected_row in selected_rows:
    #             try:
    #                 row_id = selected_row.split('_')[1]
    #                 row_id = str(int(row_id) - 1)
    #                 _ = table_id_to_linked_passage_ids[table_key][str(row_id)]
    #             except:
    #                 continue
    #
    #             selected_table_segment_list.append({"table_segment_node_id": f"{table_key}_{row_id}", "linked_passages": table_id_to_linked_passage_ids[table_key][str(row_id)]})
    #
    #     return selected_table_segment_list
    

    
    #############################################################################
    # SelectPassages                                                            #
    # Input: NL Question                                                        #
    # Input: selected_table_segment_list                                        #
    # InOut: integrated_graph                                                   #
    # ------------------------------------------------------------------------- #
    # For each selected table segment, put its content and its linked passages  #
    #   into the prompt, and get the response from the LLM.                     #
    # Parse the response, then update the `integrated_graph` with the selected  #
    #   table segments and its linked passages.                                 #
    # They are marked as `llm_based_selection`, and they are given edge scores  #
    #   of 100,000.                                                             #
    #############################################################################
    def select_passages(self, nl_question, selected_table_segment_list, integrated_graph):
        
        prompt_list = []
        table_segment_node_id_list = []
        
        # 1. Generate prompt which contains the table segment and its linked passages
        for selected_table_segment in selected_table_segment_list:
            table_segment_node_id = selected_table_segment['table_segment_node_id']
            linked_passages = selected_table_segment['linked_passages']
            table_key = table_segment_node_id.split('_')[0]
            table = self.table_key_to_content[table_key]
            table_title = table['title']
            row_id = int(table_segment_node_id.split('_')[1])
            table_rows = table['text'].split('\n')
            column_name = table_rows[0]
            row_values = table_rows[row_id+1]
            table_segment_text = column_name + '\n' + row_values
            table_segment_content = {"title": table_title, "content": table_segment_text}
            linked_passage_contents = []
            for linked_passage_info in linked_passages:
                linked_passage_id = linked_passage_info[0]
                linked_passage_type = linked_passage_info[1]
                linked_passage_contents.append({"title":linked_passage_id, "content": self.passage_key_to_content[linked_passage_id]['text']})
                
            graph = {"table_segment": table_segment_content, "linked_passages": linked_passage_contents}

            table_segment = graph['table_segment']
            table_segment_content = f"Table Title: {table_segment['title']}" + "\n" + table_segment['content'].replace(' , ', '[special tag]').replace(', ', ' | ').replace('[special tag]', ' , ')
            
            linked_passages = graph['linked_passages']
            linked_passage_contents = ""
            for linked_passage in linked_passages:
                title = linked_passage['title']
                content = linked_passage['content']
                tokenized_content = self.tokenizer.encode(content)
                trimmed_tokenized_content = tokenized_content[:128]
                trimmed_content = self.tokenizer.decode(trimmed_tokenized_content)
                linked_passage_contents += f"Title: {title}. Content: {trimmed_content}\n\n"
            contents_for_prompt = {"question": nl_question, "table_segment": table_segment_content, "linked_passages": linked_passage_contents}
            prompt = self.get_prompt(contents_for_prompt)
            prompt_list.append(prompt)
            table_segment_node_id_list.append(table_segment_node_id)
        
        # 2. Run LLM
        responses = self.llm.generate(
                prompt_list,
                vllm.SamplingParams(
                    n = 1,  # Number of output sequences to return for each prompt.
                    top_p = 0.9,  # Float that controls the cumulative probability of the top tokens to consider.
                    temperature = 0.5,  # randomness of the sampling
                    skip_special_tokens = True,  # Whether to skip special tokens in the output.
                    max_tokens = 128,  # Maximum number of tokens to generate per output sequence.
                    logprobs = 1
                ),
                use_tqdm = False
            )
        
        # 3. Parse LLM results and add the top 
        for table_segment_node_id, response in zip(table_segment_node_id_list, responses):
            selected_passage_id_list = response.outputs[0].text
            try:    selected_passage_id_list = ast.literal_eval(selected_passage_id_list)
            except: selected_passage_id_list = [selected_passage_id_list]
            
            for selected_passage_id in selected_passage_id_list:
                if selected_passage_id not in self.passage_key_to_content: continue
                                                                                                               # Score
                self.add_node(integrated_graph, 'table segment', table_segment_node_id, selected_passage_id,   1000000, 'llm_based_selection')
                self.add_node(integrated_graph, 'passage',       selected_passage_id,   table_segment_node_id, 1000000, 'llm_based_selection')
    

    
    
    

    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    #############################################################################
    # AddNode                                                                   #
    # InOut: graph                                                              #
    # Input: source_node_type                                                   #
    # Input: source_node_id                                                     #
    # Input: score                                                              #
    # Input: retrieval_type                                                     #
    # Input: source_rank (default = 0)                                          #
    # Input: target_rank (default = 0)                                          #
    # ------------------------------------------------------------------------- #
    # `graph` is a dictionary in which its key becomes a source node id and     #
    # its value is a dictionary containing the information about the nodes      #
    # linked to the source node.                                                #
    #   `type`: type of the source node (table segment | passage)               #
    #   `linked_nodes`: a list of linked nodes to the source node. Each linked  #
    #                   node is a list containing                               #
    #                       - target node id                                    #
    #                       - score                                             #
    #                       - retrieval type                                    #
    #                       - source rank                                       #
    #                       - target rank                                       #
    #############################################################################
    def add_node(self, graph, source_node_type, source_node_id, target_node_id, score, retrieval_type, source_rank = 0, target_rank = 0):
        # add source and target node
        if source_node_id not in graph:
            graph[source_node_id] = {'type': source_node_type, 'linked_nodes': [[target_node_id, score, retrieval_type, source_rank, target_rank]]}
        # add target node
        else:
            graph[source_node_id]['linked_nodes'].append([target_node_id, score, retrieval_type, source_rank, target_rank])
    
    #############################################################################
    # GetPrompt                                                                 #
    #############################################################################
    def get_prompt(self, contents_for_prompt):
        if 'linked_passages' in contents_for_prompt:
            prompt = self.select_passage_prompt.format(**contents_for_prompt)
        else:
            prompt = self.select_table_segment_prompt.format(**contents_for_prompt)
        
        return prompt
    
    
    
    
    
    #############################################################################
    # DeleteNodes                                                               #
    # InOut: graph                                                              #
    # Input: node_list                                                          #
    # ------------------------------------------------------------------------- #
    # Not used                                                                  #
    #############################################################################
    def delete_nodes(self, graph, node_list):
        # Convert node_list to a set for O(1) average time complexity lookups
        node_set = set(node_list)

        # Remove nodes and update linked nodes in one pass
        nodes_to_delete = []

        for node_id in list(graph.keys()):  # list(graph.keys()) to avoid runtime errors during deletion
            if node_id in node_set:
                del graph[node_id]
            else:
                graph[node_id]['linked_nodes'] = [linked_node for linked_node in graph[node_id]['linked_nodes'] if linked_node[0] not in node_set]
                if not graph[node_id]['linked_nodes']:
                    nodes_to_delete.append(node_id)

        # Delete nodes that are not linked to any other node
        for node_id in nodes_to_delete:
            del graph[node_id]
    
    
    
    














# def evaluate(retrieved_graph_list, qa_dataset, graph_query_engine):
def evaluate(retrieved_graph, qa_data, graph_query_engine):
    
    recall_list = []
    error_cases = {}
    table_key_to_content = graph_query_engine.table_key_to_content
    passage_key_to_content = graph_query_engine.passage_key_to_content
    filtered_retrieval_type = ['edge_reranking', "passage_node_augmentation_1"]
    
    
    # 1. Revise retrieved graph
    revised_retrieved_graph = {}
    for node_id, node_info in retrieved_graph.items():                  

        if node_info['type'] == 'table segment':
            linked_nodes = [x for x in node_info['linked_nodes'] 
                                if x[2] in filtered_retrieval_type and (x[2] == 'passage_node_augmentation_1' and (x[3] < 10) and (x[4] < 2)) 
                                    or x[2] in filtered_retrieval_type and (x[2] == 'table_segment_node_augmentation' and (x[4] < 1) and (x[3] < 1)) 
                                    or x[2] == 'edge_reranking'
                            ]
        elif node_info['type'] == 'passage':
            linked_nodes = [x for x in node_info['linked_nodes'] 
                                if x[2] in filtered_retrieval_type and (x[2] == 'table_segment_node_augmentation' and (x[3] < 1) and (x[4] < 1)) 
                                or x[2] in filtered_retrieval_type and (x[2] == 'passage_node_augmentation_1' and (x[4] < 10) and (x[3] < 2)) 
                                or x[2] == 'edge_reranking'
                            ]
        if len(linked_nodes) == 0: continue
        
        revised_retrieved_graph[node_id] = copy.deepcopy(node_info)
        revised_retrieved_graph[node_id]['linked_nodes'] = linked_nodes
        
        linked_scores = [linked_node[1] for linked_node in linked_nodes]
        node_score = max(linked_scores)
        revised_retrieved_graph[node_id]['score'] = node_score
    retrieved_graph = revised_retrieved_graph


    # 2. Evaluate with revised retrieved graph
    node_count = 0
    edge_count = 0
    answers = qa_data['answers']
    context = ""
    retrieved_table_set = set()
    retrieved_passage_set = set()
    sorted_retrieved_graph = sorted(retrieved_graph.items(), key = lambda x: x[1]['score'], reverse = True)
    
    for node_id, node_info in sorted_retrieved_graph:
        
        if node_info['type'] == 'table segment':
            
            table_id = node_id.split('_')[0]
            table = table_key_to_content[table_id]
            chunk_id = table['chunk_id']
            node_info['chunk_id'] = chunk_id
            
            if table_id not in retrieved_table_set:
                retrieved_table_set.add(table_id)
                
                if edge_count == 50:
                    continue
                
                context += table['text']
                edge_count += 1
                
            max_linked_node_id, max_score, _, _, _ = max(node_info['linked_nodes'], key=lambda x: x[1], default=(None, 0, 'edge_reranking', 0, 0))
            
            if max_linked_node_id in retrieved_passage_set:
                continue
            
            retrieved_passage_set.add(max_linked_node_id)
            passage_content = passage_key_to_content[max_linked_node_id]
            passage_text = passage_content['title'] + ' ' + passage_content['text']
            
            row_id = int(node_id.split('_')[1])
            table_rows = table['text'].split('\n')
            column_name = table_rows[0]
            row_values = table_rows[row_id+1]
            table_segment_text = column_name + '\n' + row_values
            
            edge_text = table_segment_text + '\n' + passage_text
            
            if edge_count == 50:
                continue
            
            edge_count += 1
            context += edge_text
        
        elif node_info['type'] == 'passage':

            if node_id in retrieved_passage_set:
                continue

            max_linked_node_id, max_score, _, _, _ = max(node_info['linked_nodes'], key=lambda x: x[1], default=(None, 0, 'edge_reranking', 0, 0))
            table_id = max_linked_node_id.split('_')[0]
            table = table_key_to_content[table_id]
            
            if table_id not in retrieved_table_set:
                retrieved_table_set.add(table_id)
                if edge_count == 50:
                    continue
                context += table['text']
                edge_count += 1

            row_id = int(max_linked_node_id.split('_')[1])
            table_rows = table['text'].split('\n')
            column_name = table_rows[0]
            row_values = table_rows[row_id+1]
            table_segment_text = column_name + '\n' + row_values
            
            if edge_count == 50:
                continue

            retrieved_passage_set.add(node_id)
            passage_content = passage_key_to_content[node_id]
            passage_text = passage_content['title'] + ' ' + passage_content['text']
            
            edge_text = table_segment_text + '\n' + passage_text
            context += edge_text
            edge_count += 1

        node_count += 1

    normalized_context = remove_accents_and_non_ascii(context)
    normalized_answers = [remove_accents_and_non_ascii(answer) for answer in answers]
    is_has_answer = has_answer(normalized_answers, normalized_context, SimpleTokenizer(), 'string')
    
    if is_has_answer:
        recall = 1
        error_analysis = {}
    else:
        recall = 0
        error_analysis = copy.deepcopy(qa_data)
        error_analysis['retrieved_graph'] = retrieved_graph
        error_analysis['sorted_retrieved_graph'] = sorted_retrieved_graph[:node_count]
        if  "hard_negative_ctxs" in error_analysis:
            del error_analysis["hard_negative_ctxs"]

    return recall, error_analysis

def remove_accents_and_non_ascii(text):
    # Normalize the text to decompose combined characters
    normalized_text = unicodedata.normalize('NFD', text)
    # Remove all characters that are not ASCII
    ascii_text = normalized_text.encode('ascii', 'ignore').decode('ascii')
    # Remove any remaining non-letter characters
    cleaned_text = re.sub(r'[^A-Za-z0-9\s,!.?\-]', '', ascii_text)
    return cleaned_text



def filter_fn(pid, values_to_remove):
    return pid[~torch.isin(pid, values_to_remove)].to("cuda")

def get_context(retrieved_graph, graph_query_engine):
    context = ""
    retrieved_table_set = set()
    retrieved_passage_set = set()
    sorted_retrieved_graph = sorted(retrieved_graph.items(), key=lambda x: x[1]['score'], reverse=True)
    table_key_to_content = graph_query_engine.table_key_to_content
    for node_id, node_info in sorted_retrieved_graph:
        if node_info['type'] == 'table segment':
            table_id = node_id.split('_')[0]
            table = table_key_to_content[table_id]
            
            if table_id not in retrieved_table_set:
                retrieved_table_set.add(table_id)
                context += table['text']
        
            max_linked_node_id, max_score, a, b, c = max(node_info['linked_nodes'], key=lambda x: x[1], default=(None, 0, 'edge_retrieval', 0, 0))

            if max_linked_node_id in retrieved_passage_set:
                continue
            
            retrieved_passage_set.add(max_linked_node_id)
            passage_content = graph_query_engine.passage_key_to_content[max_linked_node_id]
            passage_text = passage_content['title'] + ' ' + passage_content['text']
            
            row_id = int(node_id.split('_')[1])
            table_rows = table['text'].split('\n')
            column_name = table_rows[0]
            row_values = table_rows[row_id+1]
            table_segment_text = column_name + '\n' + row_values
            
            edge_text = table_segment_text + '\n' + passage_text
            context += edge_text
            
        elif node_info['type'] == 'passage':
            if node_id in retrieved_passage_set:
                continue
            
            retrieved_passage_set.add(node_id)
            passage_content = graph_query_engine.passage_key_to_content[node_id]
            passage_text = passage_content['title'] + ' ' + passage_content['text']
            #'Who created the series in which the character of Robert , played by actor Nonso Anozie , appeared ? [SEP] Nonso Anozie [SEP] Year, Title, Role, Notes [SEP] 2007, Prime Suspect 7 : The Final Act, Robert, Episode : Part 1'
            max_linked_node_id, max_score, _, _, _ = max(node_info['linked_nodes'], key=lambda x: x[1], default=(None, 0, 'edge_retrieval', 0, 0))
            table_id = max_linked_node_id.split('_')[0]
            table = table_key_to_content[table_id]
            
            if table_id not in retrieved_table_set:
                retrieved_table_set.add(table_id)
                context += table['text']
                
            row_id = int(max_linked_node_id.split('_')[1])
            table_rows = table['text'].split('\n')
            column_name = table_rows[0]
            row_values = table_rows[row_id+1]
            table_segment_text = column_name + '\n' + row_values
            
            edge_text = table_segment_text + '\n' + passage_text
            context += edge_text

    return context


def disablePrint():
    sys.stdout = open(os.devnull, 'w')

def enablePrint():
    sys.stdout = sys.__stdout__









if __name__ == "__main__":
    main()