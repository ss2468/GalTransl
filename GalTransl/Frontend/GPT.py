"""
GPT3.5 / 4 / New Bing 前端翻译的控制逻辑
"""

from os import makedirs, sep as os_sep
from os.path import join as joinpath
from os.path import exists as isPathExists
from os.path import getsize as getFileSize
from os.path import basename, dirname
from typing import Optional
from tqdm.asyncio import tqdm as atqdm
from asyncio import Semaphore, gather, Queue
from time import time
from GalTransl import LOGGER
from GalTransl.Backend.GPT3Translate import CGPT35Translate
from GalTransl.Backend.GPT4Translate import CGPT4Translate
from GalTransl.Backend.BingGPT4Translate import CBingGPT4Translate
from GalTransl.Backend.SakuraTranslate import CSakuraTranslate
from GalTransl.Backend.RebuildTranslate import CRebuildTranslate
from GalTransl.ConfigHelper import initDictList
from GalTransl.Loader import load_transList
from GalTransl.Dictionary import CGptDict, CNormalDic
from GalTransl.Problem import find_problems
from GalTransl.Cache import save_transCache_to_json
from GalTransl.Name import load_name_table
from GalTransl.CSerialize import update_json_with_transList, save_json
from GalTransl.Dictionary import CNormalDic, CGptDict
from GalTransl.ConfigHelper import CProjectConfig, initDictList, CProxyPool
from GalTransl.COpenAI import COpenAITokenPool
from GalTransl.Utils import get_file_list


async def doLLMTranslateSingleFile(
    semaphore: Semaphore,
    endpoint_queue: Queue,
    file_path: str,
    projectConfig: CProjectConfig,
    eng_type: str,
    pre_dic: CNormalDic,
    post_dic: CNormalDic,
    gpt_dic: CGptDict,
    tlugins: list,
    fPlugins: list,
    proxyPool: CProxyPool,
    tokenPool: COpenAITokenPool,
) -> bool:
    async with semaphore:
        if endpoint_queue is not None:
            endpoint = await endpoint_queue.get()
        try:
            st = time()
            proj_dir = projectConfig.getProjectDir()
            input_dir = projectConfig.getInputPath()
            output_dir = projectConfig.getOutputPath()
            cache_dir = projectConfig.getCachePath()
            file_name = file_path.replace(input_dir, "").lstrip(os_sep)
            file_name = file_name.replace(os_sep, "-}")
            input_file_path = file_path
            output_file_path = input_file_path.replace(input_dir, output_dir)
            output_file_dir = dirname(output_file_path)
            makedirs(output_file_dir, exist_ok=True)
            cache_file_path = joinpath(cache_dir, file_name)
            LOGGER.info(
                f"engine type: {eng_type}, file: {file_name}, start translating.."
            )

            match eng_type:
                case "gpt35-0613" | "gpt35-1106" | "gpt35-0125":
                    gptapi = CGPT35Translate(
                        projectConfig, eng_type, proxyPool, tokenPool
                    )
                case "gpt4" | "gpt4-turbo":
                    gptapi = CGPT4Translate(
                        projectConfig, eng_type, proxyPool, tokenPool
                    )
                case "newbing":
                    cookiePool: list[str] = []
                    for i in projectConfig.getBackendConfigSection("bingGPT4")[
                        "cookiePath"
                    ]:
                        cookiePool.append(joinpath(projectConfig.getProjectDir(), i))
                    gptapi = CBingGPT4Translate(projectConfig, cookiePool, proxyPool)
                case "sakura-009" | "sakura-010" | "galtransl-v1":
                    gptapi = CSakuraTranslate(
                        projectConfig, eng_type, endpoint, proxyPool
                    )
                case "rebuildr" | "rebuilda" | "dump-name":
                    gptapi = CRebuildTranslate(projectConfig, eng_type)
                case _:
                    raise ValueError(f"不支持的翻译引擎类型 {eng_type}")

            # 1、初始化trans_list
            origin_input = ""

            if getFileSize(input_file_path) == 0:
                return True
            for plugin in fPlugins:
                try:
                    origin_input = plugin.plugin_object.load_file(input_file_path)
                    save_func = plugin.plugin_object.save_file
                    break
                except TypeError as e:
                    LOGGER.error(f"{file_name} 不是插件 {plugin.name} 支持的格式：{e}")
                    return False
                except Exception as e:
                    LOGGER.error(f"插件 {plugin.name} 读取文件 {file_name} 出错: {e}")
                    return False
            if not origin_input:
                origin_input = input_file_path
                save_func = save_json

            try:
                trans_list, json_list = load_transList(origin_input)
            except Exception as e:
                LOGGER.error(f"文件 {file_name} 加载翻译列表失败: {e}")
                return False
            
            # 导出人名表功能
            if "dump-name" in eng_type:
                global name_dict
                for tran in trans_list:
                    if tran.speaker and type(tran.speaker) == str:
                        if tran.speaker not in name_dict:
                            name_dict[tran.speaker] = 0
                        name_dict[tran.speaker] += 1
                return True

            # 2、翻译前处理
            for i, tran in enumerate(trans_list):
                for plugin in tlugins:
                    try:
                        tran = plugin.plugin_object.before_src_processed(tran)
                    except Exception as e:
                        LOGGER.error(f"插件 {plugin.name} 执行失败: {e}")
                        raise e
                tran.analyse_dialogue()  # 解析是否为对话
                tran.post_jp = pre_dic.do_replace(tran.post_jp, tran)  # 译前字典替换
                if projectConfig.getDictCfgSection("usePreDictInName"):  # 译前name替换
                    if type(tran.speaker) == type(tran._speaker) == str:
                        tran.speaker = pre_dic.do_replace(tran.speaker, tran)
                for plugin in tlugins:
                    try:
                        tran = plugin.plugin_object.after_src_processed(tran)
                    except Exception as e:
                        LOGGER.error(f"插件 {plugin.name} 执行失败: {e}")
                        raise e

            # 3、读出未命中的Translate然后批量翻译

            await gptapi.batch_translate(
                file_name,
                cache_file_path,
                trans_list,
                projectConfig.getKey("gpt.numPerRequestTranslate"),
                retry_failed=projectConfig.getKey("retranslFail"),
                gpt_dic=gpt_dic,
                retran_key=projectConfig.getKey("retranslKey"),
            )
            if projectConfig.getKey("gpt.enableProofRead"):
                if "newbing" in eng_type or "gpt4" in eng_type:
                    await gptapi.batch_translate(
                        file_name,
                        cache_file_path,
                        trans_list,
                        projectConfig.getKey("gpt.numPerRequestProofRead"),
                        retry_failed=projectConfig.getKey("retranslFail"),
                        gpt_dic=gpt_dic,
                        proofread=True,
                        retran_key=projectConfig.getKey("retranslKey"),
                    )
                else:
                    LOGGER.warning("当前引擎不支持校对，跳过校对步骤")

            # 4、翻译后处理
            for i, tran in enumerate(trans_list):
                for plugin in tlugins:
                    try:
                        tran = plugin.plugin_object.before_dst_processed(tran)
                    except:
                        LOGGER.error(f"插件 {plugin.name} 执行失败: {e}")
                        raise e
                tran.recover_dialogue_symbol()  # 恢复对话框
                tran.post_zh = post_dic.do_replace(tran.post_zh, tran)  # 译后字典替换
                if projectConfig.getDictCfgSection("usePostDictInName"):  # 译后name替换
                    if tran._speaker:
                        if type(tran.speaker) == type(tran._speaker) == list:
                            tran._speaker = [
                                post_dic.do_replace(s, tran) for s in tran.speaker
                            ]
                        elif type(tran.speaker) == type(tran._speaker) == str:
                            tran._speaker = post_dic.do_replace(tran.speaker, tran)
                for plugin in tlugins:
                    try:
                        tran = plugin.plugin_object.after_dst_processed(tran)
                    except:
                        LOGGER.error(f"插件 {plugin.name} 执行失败: {e}")
                        raise e
        finally:
            if endpoint_queue is not None:
                endpoint_queue.put_nowait(endpoint)
    if eng_type != "rebuildr":
        find_problems(trans_list, projectConfig, gpt_dic)
        # 用于保存problems
        save_transCache_to_json(trans_list, cache_file_path, post_save=True)

    # 5、整理输出
    if isPathExists(joinpath(proj_dir, "人名替换表.csv")):
        name_dict = load_name_table(joinpath(proj_dir, "人名替换表.csv"))
    else:
        name_dict = {}

    new_json_list = update_json_with_transList(trans_list, json_list, name_dict)
    save_func(output_file_path, new_json_list)

    et = time()
    LOGGER.info(f"文件 {file_name} 翻译完成，用时 {et-st:.3f}s.")
    return True


async def run_task(task, progress_bar):
    result = await task  # Wait for the individual task to complete
    progress_bar.update(1)  # Update the progress bar
    return result


async def doLLMTranslate(
    projectConfig: CProjectConfig,
    tokenPool: COpenAITokenPool,
    proxyPool: Optional[CProxyPool],
    tPlugins: list,
    fPlugins: list,
    eng_type="offapi",
) -> bool:
    pre_dic_dir = projectConfig.getDictCfgSection()["preDict"]
    post_dic_dir = projectConfig.getDictCfgSection()["postDict"]
    gpt_dic_dir = projectConfig.getDictCfgSection()["gpt.dict"]
    default_dic_dir = projectConfig.getDictCfgSection()["defaultDictFolder"]
    project_dir = projectConfig.getProjectDir()
    # 加载字典
    pre_dic = CNormalDic(initDictList(pre_dic_dir, default_dic_dir, project_dir))
    post_dic = CNormalDic(initDictList(post_dic_dir, default_dic_dir, project_dir))
    gpt_dic = CGptDict(initDictList(gpt_dic_dir, default_dic_dir, project_dir))

    workersPerProject = projectConfig.getKey("workersPerProject")

    if "sakura" in eng_type or "galtransl" in eng_type:
        endpoint_queue = Queue()
        backendSpecific = projectConfig.projectConfig["backendSpecific"]
        section_name = "SakuraLLM" if "SakuraLLM" in backendSpecific else "Sakura"
        if "endpoints" in projectConfig.getBackendConfigSection(section_name):
            endpoints = projectConfig.getBackendConfigSection(section_name)["endpoints"]
        else:
            endpoints = [
                projectConfig.getBackendConfigSection(section_name)["endpoint"]
            ]
        repeated = (workersPerProject + len(endpoints) - 1) // len(endpoints)
        for _ in range(repeated):
            for endpoint in endpoints:
                endpoint_queue.put_nowait(endpoint)
        LOGGER.info(f"当前使用 {workersPerProject} 个Sakura worker引擎")
    else:
        endpoint_queue = None
    
    # 人名表初始化
    if "dump-name" in eng_type:
        global name_dict
        name_dict = {}

    file_list = get_file_list(projectConfig.getInputPath())
    if not file_list:
        raise RuntimeError(f"{projectConfig.getInputPath()}中没有待翻译的文件")
    semaphore = Semaphore(workersPerProject)
    progress_bar = atqdm(total=len(file_list), desc="Processing files")
    tasks = [
        run_task(
            doLLMTranslateSingleFile(
                semaphore,
                endpoint_queue,
                file_name,
                projectConfig,
                eng_type,
                pre_dic,
                post_dic,
                gpt_dic,
                tPlugins,
                fPlugins,
                proxyPool,
                tokenPool,
            ),
            progress_bar,
        )
        for file_name in file_list
    ]
    # await atqdm.gather(*tasks)
    await gather(*tasks)  # run
    progress_bar.close()

    if "dump-name" in eng_type:
        import csv
        proj_dir = projectConfig.getProjectDir()
        name_dict = dict(sorted(name_dict.items(), key=lambda item: item[1], reverse=True))
        with open(joinpath(proj_dir, "人名替换表.csv"), "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["JP_Name", "CN_Name", "Count"])  # 写入表头
            for name, count in name_dict.items():
                writer.writerow([name, "", count])
            LOGGER.info(f"name已保存到'人名替换表.csv'（UTF-8编码，用Emeditor编辑），填入CN_Name后可用于后续翻译name字段。")