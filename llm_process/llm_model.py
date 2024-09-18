import json
import mlx.core as mx
import mlx_lm
import os
import time
import uuid
from llama_cpp import Llama
from typing import Generator, List

from .kv_cache_manager import KVCacheManager
from .mlx_generate_stream import generate_stream
from .task_response import TaskResponse
from .logger_config import setup_logger
logger = setup_logger(__name__, level="DEBUG")

import gc

def get_mlx_params(params) -> dict:
    return {
        'temp'                    : getattr(params, 'temperature', 1.0),
        'max_tokens'              : getattr(params, 'max_tokens', 4096),
        'stream'                  : getattr(params, 'stream', False),
        'repetition_penalty'      : getattr(params, 'repetition_penalty', None),
        'repetition_context_size' : getattr(params, 'repetition_context_size', 20),
        'top_p'                   : getattr(params, 'top_p', 1.0),
        'stop'                    : getattr(params, 'stop', []),
        'kv_cache_session_id'     : getattr(params, 'kv_cache_session_id', 0),
    }

def get_llama_cpp_params(params) -> dict:
    return {
        'temperature'    : getattr(params, 'temperature', 1.0),
        'max_tokens'     : getattr(params, 'max_tokens', 4096),
        'stream'         : getattr(params, 'stream', False),
        'top_p'          : getattr(params, 'top_p', 0.95),
        'min_p'          : getattr(params, 'min_p', 0.05),
        'typical_p'      : getattr(params, 'typical_p', 1.0),
        'stop'           : getattr(params, 'stop', []),
        'frequency_penalty': getattr(params, 'frequency_penalty', 0.0),
        'presence_penalty' : getattr(params, 'presence_penalty', 0.0),
        'repeat_penalty' : getattr(params, 'repeat_penalty', 1.1),
        'top_k'          : getattr(params, 'top_k', 40),
        'seed'           : getattr(params, 'seed', None),
        'mirostat_mode'  : getattr(params, 'mirostat_mode', 2),
        'mirostat_tau'   : getattr(params, 'mirostat_tau', 5.0),
        'mirostat_eta'   : getattr(params, 'mirostat_eta', 0.1),
    }


class LLMModel:
    def __init__(self):
        self.model_path: str = ""        
        self.model_name: str = ""
        self.model_type: str = ""
        self.model_cache_limit: int = 0
        self.model = None
        self.tokenizer = None

    def load_model(self, params) -> TaskResponse:
        request_model_path = params.llm_model_path
        chat_format = params.chat_format
        logger.debug(f"start loading model: {request_model_path}.")
        start_time = time.time()
        try:
            if os.path.isfile(request_model_path):
                self.model_type = "llama-cpp"
                self.model = Llama(model_path=request_model_path, n_gpu_layers=-1, n_ctx=0, chat_format=chat_format, verbose=True)
                context_length = self.model.n_ctx()
            else:
                self.model_type = "mlx"

                tokenizer_config = {"trust_remote_code": None}

                self.model, self.tokenizer = mlx_lm.load(request_model_path, tokenizer_config=tokenizer_config)
                self.tokenizer.model_max_length
                context_length = self.get_max_position_embeddings(request_model_path)
        except Exception as e:
            logger.error(str(e))
            error_messsage = f"load failed: {request_model_path}. Reason={str(e)}"
            return TaskResponse.create(500, error_messsage)

        load_time = time.time() - start_time
        self.model_path = request_model_path
        self.model_name = os.path.basename(request_model_path)

        logger.debug(f"loadded model: {self.model_name}. time: {load_time}s")
        model_info = {"model_name": self.model_name, 
                      "model_path":self.model_path, 
                      "model_type": self.model_type, 
                      "context_length": context_length, 
                      "load_time":load_time
                      }
        logger.debug(f"{model_info=}")
        return TaskResponse.create(200, model_info)

    def get_kv_caches_info(self) -> TaskResponse:
        try:
            kv_cache_sessions = KVCacheManager.get_all_sessions_with_size()
            logger.debug(f"{kv_cache_sessions=}")
            return TaskResponse(200, f"{kv_cache_sessions=}")
        except Exception as e:
            return TaskResponse(500, e)

    def remove_kv_cache(self, params) -> TaskResponse:
        session_id = int(params.session_id)
        try:
            if not KVCacheManager.has_cache(session_id):
                return TaskResponse(404, f"{session_id=} not found.")

            KVCacheManager.remove_cache(session_id)

            if KVCacheManager.has_cache(session_id):
                return TaskResponse(500, f"server error. {session_id=} failed to remove.")

            return TaskResponse(200, f"{session_id=} successfully removed.")
        except Exception as e:
            return TaskResponse(500, e)

    def remove_old_kv_caches(self, params) -> TaskResponse:
        seconds = int(params.seconds)
        try:
            KVCacheManager.remove_old_caches(seconds)

            return TaskResponse(200, "old sessions successfully removed.")
        except Exception as e:
            return TaskResponse(500, e)


    def set_cache_liimt(self, params) -> TaskResponse:
        limit = int(params.cache_limit)
        try:
            if limit >= 0:
                logger.debug(f"set model cache limit {self.model_name=}, limit = {limit}")
                previous_limit = mx.metal.set_cache_limit(limit)
                self.model_cache_limit = limit
                message = f"change cache limit from {previous_limit} to {self.model_cache_limit}"
                logger.debug(message)
            return TaskResponse(200, message)
        except Exception as e:
            return TaskResponse(500, e)

    def get_cache_memory(self) -> TaskResponse:
        try:
            cache_memory_size = mx.metal.get_cache_memory()
            logger.debug(f"{cache_memory_size=}")
            return TaskResponse(200, f"{cache_memory_size=}")
        except Exception as e:
            return TaskResponse(500, e)
        
    def force_metal_clear_cache(self):
        logger.debug(f"mx.metal.get_cache_memory()={mx.metal.get_cache_memory()}")
        mx.metal.clear_cache()
        logger.debug(f"mx.metal.get_cache_memory()={mx.metal.get_cache_memory()}")

    def get_max_position_embeddings(self, model_path):
        with open(f"{model_path}/config.json", "r") as f:
            config = json.load(f)
            return config.get("max_position_embeddings")

    def token_count(self, params) -> TaskResponse:
        try:
            if self.model_type == 'mlx':
                if params.messages != []:
                    tokenized_input = self.tokenizer.apply_chat_template(params.messages, tokenize=True, add_generation_prompt=True)
                else:
                    tokenized_input = self.tokenizer.tokenize(params.prompt)
            elif self.model_type == 'llama-cpp':
                if params.messages != []:
                    text = json.dumps(params.messages)                
                else:
                    text = params.prompt
                text = bytes(text, 'utf-8')
                tokenized_input= self.model.tokenize(text)

            token_length = len(tokenized_input)
            return TaskResponse(200, token_length)
        except Exception as e:
            return TaskResponse(500, e)

    def completions_stream(self, params) -> Generator[TaskResponse, None, None]:
        mlx_llama_generate = MLX_LLAMA_Generate(self.model, self.tokenizer, self.model_type, self.model_name)

        logger.debug("start completions_stream")

        for response in mlx_llama_generate.generate_completion(params):

            if not isinstance(response, dict):
                error_message = f"error: generate_completion: response is not dict. {response=}"
                logger.error(error_message)
                response = error_message
                status = 500
            elif response.get("error"):
                status = 500
            else:
                status = 200
            yield TaskResponse.create(status, response)

        # logger.debug("completions_stream: Garbage Collect.")
        # del mlx_llama_generate 
        # gc.collect()

        if self.model_cache_limit > 0 and mx.metal.get_cache_memory() > int(self.model_cache_limit):
            self.force_metal_clear_cache()
   

    def apply_chat_template(self, messages: List[dict]) -> str:
        chatml_instruct_template="{%- set ns = namespace(found=false) -%}{%- for message in messages -%}{%- if message['role'] == 'system' -%}{%- set ns.found = true -%}{%- endif -%}{%- endfor -%}{%- for message in messages %}{%- if message['role'] == 'system' -%}{{- '<|im_start|>system\n' + message['content'].rstrip() + '<|im_end|>\n' -}}{%- else -%}{%- if message['role'] == 'user' -%}{{-'<|im_start|>user\n' + message['content'].rstrip() + '<|im_end|>\n'-}}{%- else -%}{{-'<|im_start|>assistant\n' + message['content'] + '<|im_end|>\n' -}}{%- endif -%}{%- endif -%}{%- endfor -%}{%- if add_generation_prompt -%}{{-'<|im_start|>assistant\n'-}}{%- endif -%}"

        try:
            chat_prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            logger.debug(f"{chat_prompt=}")
        except:
            logger.warn("apply chat template failed. try default format.")
            try:
                self.tokenizer.chat_template = self.tokenizer.default_chat_template
                chat_prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
                logger.debug(f"{chat_prompt=}")
            except:
                logger.warn("apply chat template failed. try fallback format.")
                chat_prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, chat_template=chatml_instruct_template)
                logger.debug(f"{chat_prompt=}")
        return chat_prompt


class MLX_LLAMA_Generate(LLMModel):
    def __init__(self, model, tokenizer, model_type, model_name):
        super().__init__()

        self.model = model
        self.tokenizer = tokenizer
        self.model_type = model_type
        self.model_name = model_name


    def generate_completion(self, params) -> Generator[dict, None, None]:
        def exception_message_in_generate(e: Exception, model_type: str) -> dict:
            error_message = f"Error in {model_type} generate_stream: {str(e)}"
            logger.error(error_message)
            response = {"error": error_message}
            return response

        def calculate_mlx_usage(self, prompt:str, all_tokens:List, perf_timer:List = []) -> dict:
            prompt_tokens = len(self.tokenizer.encode(prompt))
            completion_tokens = len(all_tokens)
            total_tokens = prompt_tokens + completion_tokens
            if len(perf_timer) == 3:
                prompt_eval_time = perf_timer[1] - perf_timer[0]
                generation_time  = perf_timer[2] - perf_timer[1]
            else:
                prompt_eval_time = "not measured"
                generation_time  = "not measured"

            usage = {}
            usage = {"prompt_tokens": prompt_tokens, 
                     "completion_tokens":completion_tokens, 
                     "total_tokens": total_tokens,
                     "prompt_eval_time": prompt_eval_time,
                     "generation_time" : generation_time
                    }
            return usage
        

        if self.model_type == 'mlx':
            mlx_params = get_mlx_params(params)
            request_id = str(uuid.uuid4())
            created_time = int(time.time())
            all_tokens = []
            logger.debug(f"{mlx_params=}")

            if params.apply_chat_template:
                if params.kv_cache_session_id and KVCacheManager.has_cache(params.kv_cache_session_id):
                    if len(params.messages) > 3:
                        messages = [params.messages[-1]]
                    else:
                        messages = params.messages
                else:
                    messages = params.messages

                params.prompt = self.apply_chat_template(messages)
                logger.debug(f"Chat Template applied {params.prompt=}")

            start_time = time.perf_counter()
            is_first_token = True

            try:
                for (text, tokens) in generate_stream(
                    model=self.model, 
                    tokenizer=self.tokenizer, 
                    prompt=params.prompt,
                    **mlx_params
                    ):
                    all_tokens.extend(tokens)
                    if params.apply_chat_template:
                        if params.stream:
                            response = {"id": request_id,
                                        "object": "chat.completion.chunk", 
                                        "created": created_time, 
                                        "model": self.model_name, 
                                        "choices": [{"delta": {"content": text}}]
                                        }
                        else:
                            response = {"id": request_id, 
                                        "object": "chat.completion", 
                                        "created": created_time, 
                                        "model": self.model_name, 
                                        "choices": [{"message": { "content": text}}]
                                        }
                            response["usage"] = calculate_mlx_usage(self, params.prompt, all_tokens)
                    else:
                        if params.stream:
                            response = {"id": request_id, 
                                        "object": "text_completion", 
                                        "created": created_time, 
                                        "model": self.model_name, 
                                        "choices": [{"text": text}]
                                        }
                        else:
                            response = {"id": request_id, 
                                        "object": "text_completion", 
                                        "created": created_time, 
                                        "model": self.model_name, 
                                        "choices": [{"text": text}]
                                        }
                            response["usage"] = calculate_mlx_usage(self, params.prompt, all_tokens)
                    if is_first_token:
                        prompt_eval_time = time.perf_counter()
                        is_first_token = False
                    yield response

                if params.complete_text:
                    generate_time = time.perf_counter()
                    perf_timer = [start_time, prompt_eval_time, generate_time]
                    complete_text = self.tokenizer.decode(all_tokens)
                    response = {"id": request_id, 
                                "object": "text_completion", 
                                "created": created_time, 
                                "model": self.model_name, 
                                "choices": [{"text": "", "complete_text": complete_text}]
                                }
                    response["usage"] = calculate_mlx_usage(self, params.prompt, all_tokens, perf_timer)
                    yield response
    
            except Exception as e:
                logger.error(f"Error in generate_stream: {e}, text: {text}, tokens: {tokens}")
                yield exception_message_in_generate(e, self.model_type)

        elif self.model_type == 'llama-cpp':
            llama_cpp_params = get_llama_cpp_params(params)
            logger.debug(f"{llama_cpp_params=}")
            try:
                if params.apply_chat_template:
                    if params.stream:
                        all_decoded_tokens = []
                        for response in self.model.create_chat_completion(
                            messages=params.messages,
                            **llama_cpp_params
                            ):
                            all_decoded_tokens.append(response["choices"][0]["delta"].get("content"))
                            yield response
                    else:
                        response = self.model.create_chat_completion(
                            messages=params.messages,
                            **llama_cpp_params
                            )
                        yield response

                else:
                    if params.stream:
                        all_decoded_tokens = []
                        for response in self.model.create_completion(
                            prompt=params.prompt,
                            **llama_cpp_params
                            ):
                            all_decoded_tokens.append(response["choices"][0]["text"])
                            yield response
                    else:
                        response = self.model.create_completion(
                            prompt=params.prompt,
                            **llama_cpp_params
                            )
                        yield response

                if params.complete_text:
                    all_str_tokens = [token for token in all_decoded_tokens if isinstance(token, str)]
                    complete_text = "".join(all_str_tokens)
                    response = {"id": response["id"], 
                                "object":response["object"], 
                                "created": response["created"], 
                                "model": response["model"],
                                "choices": [{"text": "", "complete_text": complete_text}]
                                }
                    response["usage"] = {"prompt_tokens":0, "completion_tokens":len(all_decoded_tokens), "total_tokens": len(all_decoded_tokens)}
                    yield response

            except Exception as e:
                yield exception_message_in_generate(e, self.model_type)

        if params.stream:
            response = {"stream_done": True}
            yield response
