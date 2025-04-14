import json, time, asyncio, os, traceback
from turtle import title
from opencc import OpenCC
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from alive_progress import alive_bar
from GalTransl.COpenAI import COpenAITokenPool
from GalTransl.ConfigHelper import CProxyPool
from GalTransl import LOGGER, LANG_SUPPORTED
from GalTransl.i18n import get_text,GT_LANG
from sys import exit
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.Dictionary import CGptDict
from GalTransl.Utils import contains_katakana,is_all_chinese,decompress_file_lzma
from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.Backend.Prompts import GENDIC_PROMPT,GENDIC_SYSTEM
import collections
from typing import List, Set, Dict, Optional
from threading import Lock

class GenDic(BaseTranslate):
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
        proxy_pool: Optional[CProxyPool],
        token_pool: COpenAITokenPool,
    ):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        self.dic_counter=collections.Counter()
        self.dic_list=[]
        self.wokers=config.getKey("workersPerProject")
        self.counter_lock = Lock()
        self.list_lock = Lock()
        self.config=config
        pass



    async def llm_gen_dic(self,text:str):
        prompt=GENDIC_PROMPT.format(input=text)
        rsp=self.ask_chatbot(prompt,GENDIC_SYSTEM)
        print(rsp)
        lines=rsp.split("\n")

        for line in lines:
            sp=line.split("\t")
            if len(sp)<3:
                continue
            if "日文" in sp[0]:
                continue
            src=sp[0]
            dst=sp[1]
            note=sp[2]
            with self.counter_lock:
                if src in self.dic_counter:
                    self.dic_counter[src]+=1
                else:
                    self.dic_counter[src]=1
                    with self.list_lock:
                        self.dic_list.append([src,dst,note])


    async def batch_translate(
        self,
        json_list: list,
    ) -> bool:

        with alive_bar(title="载入分词……") as bar:
            # get tmp dir
            import tempfile
            tmp_dir=tempfile.gettempdir()
            model_path=os.path.join(tmp_dir,"bccwj-suw+unidic_pos+pron.model")
            if not os.path.exists(model_path):
                zst_path="./res/bccwj-suw+unidic_pos+pron.model.xz"
                decompress_file_lzma(zst_path,model_path)
            bar()
            import vaporetto
            try:
                with open(model_path, 'rb') as fp:
                    model = fp.read()
                tokenizer = vaporetto.Vaporetto(model, predict_tags = True)
            except Exception as e:
                LOGGER.error(e)
                LOGGER.error("载入分词模型失败，请尝试重启程序")
                os.remove(model_path)
                return False
            bar()

            segment_list=[]
            max_len=512
            tmp_text=""
            for item in json_list:
                if len(tmp_text)>max_len:
                    segment_list.append(tmp_text)
                    tmp_text=""
                if "name" not in item:
                    tmp_text+=item["message"]+"\n"
                else:
                    tmp_text+=item["name"]+item["message"]+"\n"
            
            segment_list.append(tmp_text)
            segment_words_list=[]
            word_counter=collections.Counter()
            bar.title="处理分词……"

            for item in segment_list:
                tmp_words=set()
                tokens = tokenizer.tokenize(item)
                for token in tokens:
                    surf=token.surface()
                    tag=token.tag(0)
                    if len(surf)<=1:
                        continue
                    if is_all_chinese(surf):
                        continue
                    if tag is None:
                        if contains_katakana(surf):
                            tmp_words.add(surf)
                            word_counter[surf]+=1
                    if tag and "固有名詞" in tag:
                        tmp_words.add(surf)
                        word_counter[surf]+=1
                segment_words_list.append(tmp_words)
                bar()
        
        # 剔除出现次数小于2的词语
        word_counter = {word: count for word, count in word_counter.items() if count >= 2}
        segment_words_list_new=[]
        for item in segment_words_list:
            item_new=set()
            for word in item:
                if word in word_counter:
                    item_new.add(word)
            segment_words_list_new.append(item_new)

        index_list=solve_sentence_selection(segment_words_list_new)
        # 取前100个
        index_list=index_list[:100]
        LOGGER.info(f"启动{self.wokers}个工作线程，共{len(index_list)}个任务" )
        with alive_bar(total=len(index_list),title="生成字典……") as bar:
            # 定义工作函数
            def process_item(idx):
                try:
                    item = segment_list[idx]
                    # 由于llm_gen_dic是异步函数，需要在同步环境中运行
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    result = loop.run_until_complete(self.llm_gen_dic(item))
                    loop.close()
                    bar()
                    return result
                except Exception as e:
                    LOGGER.error(f"处理任务时出错: {e}")
                    return None
            
            # 使用ThreadPoolExecutor处理任务
            with ThreadPoolExecutor(max_workers=self.wokers) as executor:
                # 提交所有任务
                futures = [executor.submit(process_item, idx) for idx in index_list]
                
                # 等待所有任务完成
                for future in futures:
                    future.result()  # 获取结果，确保所有异常都被处理


        # 保存到文件
        # 按出现次数排序
        self.dic_list.sort(key=lambda x: self.dic_counter[x[0]], reverse=True)
        final_list=[]
        # 过滤只出现1次的词语
        for item in self.dic_list:
            if "NULL" in item[0]:
                continue
            if self.dic_counter[item[0]]>1:
                final_list.append(item)
            elif "人名" in item[2]:
                final_list.append(item)
            elif "地名" in item[2]:
                final_list.append(item)

        result_path=os.path.join(self.config.getProjectDir(),"项目GPT字典-生成.txt")

        with open(result_path, "w", encoding="utf-8") as f:
            f.write("日文原词\t中文\t备注\n")
            for item in final_list:
                f.write(item[0]+"\t"+item[1]+"\t"+item[2]+"\n")
        LOGGER.info(f"字典生成完成，共{len(final_list)}个词语，保存到{result_path}")

        return True



def solve_sentence_selection(sentences):
    all_words = set()
    for sentence in sentences:
        all_words.update(sentence)

    covered_words = set()
    selected_indices = []
    remaining_sentences_indices = list(range(len(sentences)))

    while covered_words != all_words and remaining_sentences_indices:
        best_sentence_index = -1
        max_new_coverage = -1

        for index in remaining_sentences_indices:
            sentence = sentences[index]
            new_coverage = len(sentence - covered_words)

            if new_coverage > max_new_coverage:
                max_new_coverage = new_coverage
                best_sentence_index = index

        if best_sentence_index != -1:
            best_sentence = sentences[best_sentence_index]
            covered_words.update(best_sentence)
            selected_indices.append(best_sentence_index)
            remaining_sentences_indices.remove(best_sentence_index)
        else:
            break

    return selected_indices

