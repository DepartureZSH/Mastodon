# fetcher/reblog_favourite.py
from _ast import arg
import requests
import time
import re
from datetime import datetime, timezone, timedelta
import argparse
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from multiprocessing import Process
import logging
import random
from utils import judge_sleep_limit_table, judge_api_islimit, save_error_log, create_unique_index
from config import Config

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()  
handler.setLevel(logging.DEBUG)    
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


limit_dict = {}
limit_set = set()

def get_favourite_boost(instance, status_id, headers, local_collections,worker_id):
    """
    Fetches reblogs and favourites for a specific status.
    
    Args:
        instance (str): Mastodon instance name.
        status_id (str): ID of the status (tweet).
        headers (dict): HTTP headers for the request.
        local_collections (dict): Local MongoDB collections.
    
    Returns:
        bool: True if successful, False otherwise.
    """
    reblog_url = f"https://{instance}/api/v1/statuses/{status_id}/reblogged_by"
    favourite_url = f"https://{instance}/api/v1/statuses/{status_id}/favourited_by"

    reblogs = []
    favourites = []
    last_page_flag = -1
    retry_thresh = 3
    
    for url, storage in [(reblog_url, reblogs), (favourite_url, favourites)]:
        retry_time = 0
        while True:
            params = {'limit': 40}
            if last_page_flag != -1:
                params['max_id'] = last_page_flag
            try:
                response = requests.get(url, headers=headers, params=params, timeout=5)
                if response.status_code == 200:
                    res_headers = {k.lower(): v for k, v in response.headers.items()}
                    judge_sleep_limit_table(res_headers, instance,limit_dict,limit_set)
                    data = response.json()
                    storage.extend(data)
                    if 'link' not in res_headers or len(data) < 40:
                        break
                    match = re.search(r'max_id=(\d+)', res_headers.get('link', ''))
                    if match:
                        last_page_flag = match.group(1)
                    else:
                        break
                    retry_time = 0
                elif response.status_code in [503, 429]:
                    retry_time += 1
                    time.sleep(random.random())
                    logger.warning("Encountered 429 or 503 error, retrying...")
                    if retry_time > retry_thresh:
                        limit_set.add(instance)
                        limit_dict[instance] = datetime.now(timezone.utc) + timedelta(minutes=5)
                        save_error_log(local_collections['error_log'], "booster_favouriter", f"{instance}#{status_id}", "429or503", error_message=response.text)
                        return False
                elif response.status_code in [401, 403, 404]:
                    logger.error(f"Error :{response.status_code},worker_id: {worker_id},instance: {instance}")
                    return False
                else:
                    save_error_log(local_collections['error_log'], "booster_favouriter", f"{instance}#{status_id}", "Error", res_code=response.status_code, error_message=response.text)
                    logger.error(f"Error fetching reblogs/favourites for {instance}#{status_id}: {response.status_code}")
                    return False

            #except requests.exceptions.Timeout:
            #    retry_time += 1
            #    time.sleep(random.random())
            #    logger.warning("Request timed out, retrying...")
            #    if retry_time > retry_thresh:
            #        save_error_log(local_collections['error_log'], "booster_favouriter", f"{instance}#{status_id}", "TimeOut")
            except Exception as e:
                save_error_log(local_collections['error_log'], "booster_favouriter", f"{instance}#{status_id}", "Error", error_message=str(e))
                logger.exception(f"Exception while connecting to {instance}#{status_id}: {e}")
                return False

    if reblogs or favourites:
        sid = f"{instance}#{status_id}"
        try:
            local_collections['boostersfavourites'].insert_one({
                "sid": sid,
                "reblogs": reblogs,
                "favourites": favourites
            })
            logger.info(f"Successfully saved reblogs and favourites for {sid}.")
            return True
        except DuplicateKeyError:
            logger.warning(f"Reblogs and favourites for {sid} already exist, skipping.")
            return True
        except Exception as e:
            logger.error(f"Error saving reblogs/favourites for {sid}: {e}")
            return False
    else:
        logger.warning(f"No reblogs or favourites found for {instance}#{status_id}")
        return False


def fetch_status_id(local_livefeeds_collection, limit_set, local_collections, retry_thresh=10):
    """
    Fetches a status ID from the livefeeds collection that is pending processing.
    
    Args:
        local_livefeeds_collection (pymongo.collection.Collection): The livefeeds collection.
        limit_set (set): Set of instances under rate limit.
        local_collections (dict): Local MongoDB collections.
        retry_thresh (int, optional): Retry threshold. Defaults to 10.
    
    Returns:
        dict or None: The status information or None if not found.
    """
    retry_time = 0
    while True:
        judge_api_islimit(limit_dict,limit_set)
        candidates = list(local_livefeeds_collection.find(
            {
                "status": "pending",
                "instance_name": {"$nin": list(limit_set)}
            }
        ).limit(5))
        if not candidates and not limit_set:
            logger.info("No eligible statuses found and limit_set is empty. Terminating task.")
            return None
        for candidate in candidates:
            batch = local_livefeeds_collection.find_one_and_update(
                {"_id": candidate["_id"], "status": "pending"},
                {"$set": {"status": "read"}}
            )
            if batch:
                logger.info(f"Found status ID: {batch['instance_name']}#{batch['id']}")
                return batch
        logger.info(f"No matching statuses found, retrying... Attempt {retry_time}")
        time.sleep(2)
        retry_time += 1
        if retry_time >= retry_thresh:
            return None

def process_task(worker_id, config, mongo_args, tokens, terminate_flag):
    """
    Worker process task for fetching reblogs and favourites.

    Args:
        worker_id (int): ID of this worker.
        config (Config): Configuration object.
        local_collections (dict): Local MongoDB collections.
        tokens (list): List of API tokens.
        terminate_flag (dict): Dictionary flag to terminate processes.
    """
    no_pending_counter = 0  # Counter to track consecutive iterations with no pending statuses
    inactivity_threshold = 10  # Number of consecutive iterations without pending statuses before termination


    local_client = MongoClient(mongo_args['local_mongo_uri'])
    local_db = local_client[mongo_args['db_name']]
    local_livefeeds_collection = local_db[mongo_args['collections_names']['livefeeds']]
    local_error_collection = local_db[mongo_args['collections_names']['error_log']]
    local_boostersfavourites_collection = local_db[mongo_args['collections_names']['boostersfavourites']]
    create_unique_index(local_boostersfavourites_collection, 'sid')

    local_collections = {
        'livefeeds': local_livefeeds_collection,
        'error_log': local_error_collection,
        'boostersfavourites': local_boostersfavourites_collection
    }

    while not terminate_flag['terminate']:
        try:
            info = fetch_status_id(local_collections['livefeeds'], limit_set, local_collections)
            if info:
                # Reset counter if a pending status is found
                no_pending_counter = 0
                current_token_index = worker_id % len(tokens)
                token = tokens[current_token_index]
                headers = {'Authorization': f'Bearer {token}'}
                try:
                    success = get_favourite_boost(info['instance_name'], info['id'], headers, local_collections,worker_id)
                except Exception as e:
                    logger.error(f"Error processing {info['instance_name']}#{info['id']}: {e}")
                    success = False
                logger.info(f"Worker {worker_id} success: {success}")
                local_collections['livefeeds'].update_one(
                    {"_id": info["_id"]},
                    {"$set": {"status": "processed" if success else "fail"}}
                )
                continue
            else:
                no_pending_counter += 1
                logger.info(f"No pending statuses found, sleeping... (attempt {no_pending_counter})")
                time.sleep(60)
                if no_pending_counter >= inactivity_threshold:
                    logger.info("No pending statuses found for a prolonged period. Terminating process.")
                    terminate_flag['terminate'] = True
                    break
        except Exception as e:
            logger.exception(f"Exception during processing: {e}")
            time.sleep(5)
    local_client.close()


def main():
    """
    Main function to parse arguments and start worker processes.
    """
    parser = argparse.ArgumentParser(description='Mastodon Reblog and Favourite Worker')
    parser.add_argument('--processnum', type=int, default=1, help='Number of parallel processes')
    parser.add_argument('--id', type=int, default=1, help='Number of parallel processes')
    args = parser.parse_args()
    
    config = Config()

    
    local_mongodb_uri = config.get_local_mongodb_uri()
    #local_client = MongoClient(local_mongodb_uri)
    #local_db = local_client['mastodon']
    #local_livefeeds_collection = local_db['livefeeds']
    #local_error_collection = local_db['error_log']
    #local_boostersfavourites_collection = local_db['boostersfavourites']
    
    #create_unique_index(local_boostersfavourites_collection, 'sid')
    
    with open(config.paths.get('token_list', 'tokens/token_list.txt'), 'r', encoding='utf-8') as f:
        tokens = [line.strip() for line in f if line.strip()]
    # 在 main 函数中打印 tokens
    print("Loaded Tokens:", [f"{token[:5]}...{token[-5:]}" for token in tokens])
    #local_collections = {
    #    'livefeeds': local_livefeeds_collection,
    #    'error_log': local_error_collection,
    #    'boostersfavourites': local_boostersfavourites_collection
    #}
    process_args = {
        'local_mongo_uri': local_mongodb_uri,
        'db_name': 'mastodon',
        'collections_names': {
            'livefeeds': 'livefeeds',
            'error_log': 'error_log',
            'boostersfavourites': 'boostersfavourites'
        },
    }
    
    terminate_flag = {'terminate': False}
    
    process_list = []
    for i in range(args.processnum):
        p = Process(target=process_task, args=(i, config, process_args, tokens, terminate_flag))
        p.start()
        process_list.append(p)
    
    try:
        for p in process_list:
            p.join()
    except KeyboardInterrupt:
        terminate_flag['terminate'] = True
        for p in process_list:
            p.terminate()
        logger.info("Terminated all processes.")

    #local_client.close()
    logger.info("Reblog and Favourite Worker task completed.")

if __name__ == "__main__":
    main()
