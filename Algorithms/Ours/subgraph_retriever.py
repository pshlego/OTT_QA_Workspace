import hydra
import logging
import json
import pickle
import faiss
import time
import numpy as np
from pymongo import MongoClient
from tqdm import tqdm
from omegaconf import DictConfig, OmegaConf
from dpr.models import init_biencoder_components
from dpr.options import setup_logger, setup_cfg_gpu, set_cfg_params_from_state
from dpr.utils.model_utils import (
    setup_for_distributed_mode,
    get_model_obj,
    load_states_from_checkpoint,
)
from dpr.utils.tokenizers import SimpleTokenizer
from dpr.data.qa_validation import has_answer
from DPR.run_chain_of_skills_hotpot import generate_question_vectors

logger = logging.getLogger()
setup_logger(logger)

def set_up_encoder(cfg, sequence_length = 512):
    cfg = setup_cfg_gpu(cfg)
    logger.info("CFG (after gpu  configuration):")
    logger.info("%s", OmegaConf.to_yaml(cfg))

    saved_state = load_states_from_checkpoint(cfg.model_file)
    if saved_state.encoder_params['pretrained_file'] is not None:
        print ('the pretrained file is not None and set to None', saved_state.encoder_params['pretrained_file'])
        saved_state.encoder_params['pretrained_file'] = None
    set_cfg_params_from_state(saved_state.encoder_params, cfg)
    print ('because of loading model file, setting pretrained model cfg to bert', cfg.encoder.pretrained_model_cfg)
    cfg.encoder.pretrained_model_cfg = 'bert-base-uncased'
    cfg.encoder.encoder_model_type = 'hf_cos'
    if sequence_length is not None:
        cfg.encoder.sequence_length = sequence_length
    tensorizer, encoder, _ = init_biencoder_components(
        cfg.encoder.encoder_model_type, cfg, inference_only=True
    )

    encoder_path = cfg.encoder_path
    if encoder_path:
        logger.info("Selecting encoder: %s", encoder_path)
        encoder = getattr(encoder, encoder_path)
    else:
        logger.info("Selecting standard question encoder")
        encoder = encoder.question_model

    encoder, _ = setup_for_distributed_mode(
        encoder, None, cfg.device, cfg.n_gpu, cfg.local_rank, cfg.fp16
    )
    encoder.eval()

    # load weights from the model file
    model_to_load = get_model_obj(encoder)
    logger.info("Loading saved model state ...")

    encoder_prefix = (encoder_path if encoder_path else "question_model") + "."
    prefix_len = len(encoder_prefix)

    logger.info("Encoder state prefix %s", encoder_prefix)
    question_encoder_state = {
        key[prefix_len:]: value
        for (key, value) in saved_state.model_dict.items()
        if key.startswith(encoder_prefix)
    }
    # TODO: long term HF state compatibility fix
    if 'encoder.embeddings.position_ids' in question_encoder_state:
        if 'encoder.embeddings.position_ids' not in model_to_load.state_dict():
            del question_encoder_state['encoder.embeddings.position_ids']        
    else:
        if 'encoder.embeddings.position_ids' in model_to_load.state_dict():
            question_encoder_state['encoder.embeddings.position_ids'] = model_to_load.state_dict()['encoder.embeddings.position_ids']     
    model_to_load.load_state_dict(question_encoder_state, strict=True)
    vector_size = model_to_load.get_out_size()
    logger.info("Encoder vector_size=%d", vector_size)

    # get questions & answers
    all_context_vecs = []
    for embedding_file_path in cfg.embedding_file_path_list:
        with open(embedding_file_path, 'rb') as file:
            embedding_file = pickle.load(file)
            all_context_vecs.extend(embedding_file)

    all_context_embeds = np.array([line[1] for line in all_context_vecs]).astype('float32')
    doc_ids = [line[0] for line in all_context_vecs]
    ngpus = faiss.get_num_gpus()
    print("number of GPUs:", ngpus)
    index_flat = faiss.IndexFlatIP(all_context_embeds.shape[1]) 
    co = faiss.GpuMultipleClonerOptions()
    co.shard = True
    gpu_index_flat = faiss.index_cpu_to_all_gpus(index_flat, co, ngpu=ngpus)
    gpu_index_flat.add(all_context_embeds)
    
    return encoder, tensorizer, gpu_index_flat, doc_ids

def build_query(filename):
    data = json.load(open(filename))
    for sample in data:
        if 'hard_negative_ctxs' in sample:
            del sample['hard_negative_ctxs']
    return data 

def sort_page_ids_by_scores(page_ids, page_scores):
    # Combine page IDs and scores into a list of tuples
    combined_list = list(zip(page_ids, page_scores))

    # Sort the combined list by scores in descending order
    sorted_by_score = sorted(combined_list, key=lambda x: x[1], reverse=True)

    # Extract the sorted page IDs
    sorted_page_ids = [page_id for page_id, _ in sorted_by_score]

    return sorted_page_ids

@hydra.main(config_path="conf", config_name="subgraph_retriever")
def main(cfg: DictConfig):
    # mongodb setup
    client = MongoClient(f"mongodb://localhost:{cfg.port}/", username=cfg.username, password=str(cfg.password))
    mongodb = client[cfg.dbname]
    
    # load dataset
    all_tables = json.load(open(cfg.table_data_file_path))
    all_passages = {doc['chunk_id']:doc for doc in json.load(open(cfg.passage_data_file_path))}
    graphs = {}
    
    for graph_collection_name in cfg.graph_collection_name_list:
        graph_collection = mongodb[graph_collection_name]
        total_graphs = graph_collection.count_documents({})
        print(f"Loading {total_graphs} graphs...")
        
        for doc in tqdm(graph_collection.find(), total=total_graphs):
            doc['table_name'] = all_tables[doc['table_id']]['chunk_id']
            graphs[doc['chunk_id']] = doc

    print("finish loading graphs")

    # load encoder and index
    encoder, tensorizer, gpu_index_flat, doc_ids = set_up_encoder(cfg)
    print('encoder set up')
    
    limits = [1, 5, 10, 20, 50, 100]

    answer_recall = [0]*len(limits)
    
    expert_id = None
    if cfg.encoder.use_moe:
        logger.info("Setting expert_id=0")
        expert_id = 0
        logger.info(f"mean pool {cfg.mean_pool}")
    
    data = build_query(cfg.qa_dataset_path)
    questions_tensor = generate_question_vectors(encoder, tensorizer,
        [s['question'] for s in data], cfg.batch_size, expert_id=expert_id, mean_pool=cfg.mean_pool
    )
    
    assert questions_tensor.shape[0] == len(data)
    
    k = 100                         
    b_size = 1
    tokenizer = SimpleTokenizer()
    new_data = []
    time_list = []
    
    for i in tqdm(range(0, len(data), b_size)):
        time1 = time.time()
        questions_tensor = generate_question_vectors(encoder, tensorizer, [data[i]['question']], b_size, expert_id=expert_id, mean_pool=cfg.mean_pool, silence=True)[:1]
        D, I = gpu_index_flat.search(questions_tensor.cpu().numpy(), 500)
        original_sample = data[i]
        all_included = []
        exist_table = {}
        exist_passage = {}
        
        for j, ind in enumerate(I):
            for m, id in enumerate(ind):
                subgraph = graphs[doc_ids[id]]
                table_id = subgraph['table_id']
                table = all_tables[table_id]
                table_title = table['title']
                table_name = subgraph['table_name']
                full_text = table['text']
                rows = table['text'].split('\n')[1:]
                header = table['text'].split('\n')[0]
                doc_id = subgraph['chunk_id']
                row_id = int(doc_ids[id].split('_')[1])

                # star case
                if 'passage_score_list' in subgraph:
                    passage_id_list = sort_page_ids_by_scores(subgraph['passage_chunk_id_list'], subgraph['passage_score_list'])
                    level = 'star'
                else:
                    passage_id_list = subgraph['passage_chunk_id_list']
                    level = 'edge'
                    
                if len(passage_id_list)==0:
                    continue

                if table_id not in exist_table:
                    exist_table[table_id] = 1
                    
                    if 'answers' in original_sample:
                        pasg_has_answer = has_answer(original_sample['answers'], full_text, tokenizer, 'string')
                    else:
                        pasg_has_answer = False

                    if len(all_included) == k:
                        break
                    
                    all_included.append({'id':table_id, 'graph_id':doc_id, 'title': table_title, 'text': full_text, 'has_answer': pasg_has_answer, 'score': float(D[j][m]), 'table_name': table_name, 'linked_passage_list': passage_id_list, 'hierarchical_level': level})
                
                for passage_id in passage_id_list:
                    
                    if passage_id in exist_passage:
                        continue
                    
                    exist_passage[passage_id] = 1
                    passage = all_passages[passage_id]
                    full_text = header + '\n' + rows[row_id] + '\n' + passage['title'] + ' ' + passage['text']

                    if 'answers' in original_sample:
                        pasg_has_answer = has_answer(original_sample['answers'], full_text, tokenizer, 'string')
                    else:
                        pasg_has_answer = False
                    
                    if len(all_included) == k:
                        break
                    
                    all_included.append({'id':table_id, 'graph_id':doc_id, 'title': table_title, 'text': full_text, 'has_answer': pasg_has_answer, 'score': float(D[j][m]), 'table_name': table_name, 'linked_passage_name': passage_id, 'hierarchical_level': level})
                
                if len(all_included) == k:
                    break
        
        original_sample['ctxs'] = all_included
        time2 = time.time()
        time_list.append(time2-time1)
        
        for l, limit in enumerate(limits):
            
            if any([ctx['has_answer'] for ctx in all_included[:limit]]):
                answer_recall[l] += 1

        new_data.append(original_sample)
    
    for l, limit in enumerate(limits):
        print ('answer recall', limit, answer_recall[l]/len(data))
    print ('average time', np.mean(time_list))
    
    result_path = cfg.result_path.split('.')[0] + '_' + cfg.hierarchical_level + '.json'
    time_path = cfg.result_path.split('.')[0] + '_' + cfg.hierarchical_level + '_time.json'
    
    with open(result_path, 'w') as f:
        json.dump(new_data, f, indent=4)
    with open(time_path, 'w') as f:
        json.dump(time_list, f, indent=4)
        
if __name__ == "__main__":
    main()