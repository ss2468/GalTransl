from GalTransl.CSentense import *
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.Dictionary import CGptDict
from GalTransl.Cache import get_transCache_from_json_new
from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl import LOGGER
from GalTransl.i18n import get_text,GT_LANG


class CRebuildTranslate(BaseTranslate):
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
    ):
        pass

    def init(self) -> bool:
        """
        call it before jobs
        """
        pass

    async def asyncTranslate(self, content: CTransList, gptdict="") -> CTransList:
        """
        translate with async requests
        """
        pass

    async def batch_translate(
        self,
        filename,
        cache_path,
        trans_list: CTransList,
        num_pre_req: int,
        retry_failed: bool = False,
        gpt_dic: CGptDict = None,
        proofread: bool = False,
        retran_key: str = "",
    ) -> CTransList:
        trans_list_hit, _ = get_transCache_from_json_new(
            trans_list,
            cache_path,
            retry_failed=retry_failed,
            retran_key=retran_key,
            ignr_post_jp=True,
        )

        if len(trans_list_hit) != len(trans_list):  # 不Build
            error_msg = get_text("cache_incomplete", GT_LANG, filename)
            LOGGER.error(error_msg)
            raise Exception(error_msg)

        return trans_list_hit
