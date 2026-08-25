# -*- coding: utf-8 -*-
"""通用搜索工具 Mock 服务（bocomadp 三个搜索工具联调用）。

在单一端口上同时 mock：
  - online_search   （联网搜索，JSON POST）
  - personal_search （个人知识库搜索，multipart/form-data：REQ_MESSAGE=JSON）
  - vector_search   （行内搜索，JSON POST）

工具侧的 api_url 来自 config.yaml 的环境变量引用，联调时把它们指到本服务：
    export CUSTOM_SEARCH_API_URL="http://127.0.0.1:8001/online"
    export PERSONAL_SEARCH_API_URL="http://127.0.0.1:8001/personal"
    export VECTOR_SEARCH_API_URL="http://127.0.0.1:8001/vector"
再启动 bocomadp 应用即可（无需改 config.yaml）。

按路径分流（/online /personal /vector），比按 body 特征判断更直观、更稳。

用法：
    python mock_search_server.py            # 默认 0.0.0.0:8001
    python mock_search_server.py --port 9000
"""
from __future__ import annotations

import argparse
import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("mock-search")

app = FastAPI(title="BocomADP Search Tools Mock")

# 三个工具都要求 RSP_HEAD.TRAN_SUCCESS == "1"，否则会被判失败。
# 每类返回不同的 RSP_BODY.result 结构以贴合各工具归一化字段。


def _online_payload() -> dict:
    """联网搜索：按 score 降序会被截断（online_search 响应 8 字段）。"""
    return {
        "RSP_HEAD": {"TRAN_SUCCESS": "1", "PROCESS_STATUS_CODE": "0000"},
        "RSP_BODY": {
            "result": [
                {
                    "title": "Mock 联网结果：金融科技趋势 2026",
                    "content": "这是一条来自 mock 服务器的联网搜索结果内容。",
                    "score": "0.98",
                    "url": "https://mock.example/online/1",
                    "source": "mock-web",
                    "createTime": "2026-08-20 10:00:00",
                    "repository": "online-search",
                    "question": "金融科技趋势",
                },
                {
                    "title": "Mock 联网结果：商业银行数字化",
                    "content": "第二条 mock 联网结果摘要。",
                    "score": "0.75",
                    "url": "https://mock.example/online/2",
                },
            ]
        },
    }


def _personal_payload() -> dict:
    """个人知识库搜索：照抄真实接口响应。"""
    return {
        "RSP_BODY": {
            "result": [
                {
                    "question": "个人银行还款",
                    "url": "http://12.244.167.46/knowledge/#/kn/detail?fileId=File829069184224262&knType=1&userId=FTtLMXDw8UP9%2Fd6lcPCyqw%3D%3D&userCode=NCTCO0YWuYf2%2FuNAP2qlkQ%3D%3D&loginName=VSrFRXn50nsIbw0Wbfj8eQ%3D%3D&source=1004",
                    "content": "【个人手机银行】贷款-还贷款.docx\n个人手机银行还贷款-功能介绍\n个人手机银行还贷款-功能介绍:（一）功能入口：贷款首页-还贷款。贷款首页的小图标，根据客群特色显示，如果没有在首页找到还贷款图标的，可在搜一搜中搜索“还贷款”进入。（二）“还贷款”功能支持（手机银行上允许还款的）全品种贷款的提前、到期、逾期各类单笔还款：（房贷提前还款请注意下方提示）（1）单笔到期还款（还款日至宽限期最后一天）：支持所有贷款品种，页面会有差异化展示；（2）单笔逾期还款：支持所有贷款品种，页面会有差异化展示；（3）合并逾期还款：仅支持惠民贷产品，惠民贷借款笔数大于1笔可使用此类还款方式。（4）单笔提前还款：即缩额还款，支持所有贷款品种；注意：如遇客户咨询房贷提前还款的所有相关事宜（含手机银行个人住房贷款提前还款功能，如：是否可以直接在手机银行申请房贷提前还款，申请后如何处理等），均请勿直接对外引导或指导，请按照《新一代客服受理流程—住房按揭贷款》进行处理。（5）合并提前还款：仅支持惠民贷产品，惠民贷借款笔数大于1笔可使用此类还款方式。\n个人手机银行还贷款-页面展示逻辑\n个人手机银行还贷款-页面展示逻辑:（1）用户同时有多贷款品种逾期或到期贷款，则分别展示逾期和到期品种；若同一个贷款品种同时存在到期和逾期状态，则需先展示逾期贷款，还清后再展示到期贷款；（2）用户只有到期或逾期贷款，则展示对应贷款信息；（3）用户名下还款未结清（状态正常），则展示近期待还信息。（4）用户名下无贷款记录或贷款已结清，则展示空页面。\n个人手机银行还贷款-操作步骤\n个人手机银行还贷款-操作步骤:（一）贷款首页的小图标，根据客群特色显示，如果没有在首页找到还贷款图标的，可在搜一搜中搜索“还贷款”进入。“还贷款”进入后，根据用户自身情况进入对应待还款信息页。（二）点击【立即还款】可进入对应贷款的到期、逾期还款流程；温馨提示：如客户转入资金后，如果由于贷款逾期被冻结，在尚未批量扣款前客户主动操作“逾期还款”，会优先使用冻结的资金。（三）点击【更多】可进入对应产品的贷后信息页面查看所有借款信息。",
                    "title": "【个人手机银行】贷款-还贷款.docx-1",
                    "score": "0.6489510000000001",
                    "docId": "File829069184224262",
                    "repository": "personal-search",
                    "sourceType": "WDZS",
                    "absContent": "【个人手机银行】贷款-还贷款.docx\n个人手机银行还贷款-功能介绍\n个人手机银行还贷款-功能介绍:（一）功能入口：贷款首页-还贷款。贷款首页的小图标，根据客群特色显示，如果没有在首页找到还贷款图标的，可在搜一搜中搜索“还贷款”进入。（二）“还贷款”功能支持（手机银行上允许还款的）全品种贷款的提前、到期、逾期各类单笔还款：（房贷提前还款请注意下方提示）（1）单笔到期还款（还款日至宽限期最后一天）：支持所有贷款品种，页面会有差异化展示；（2）单笔逾期还款：支持所有贷款品种，页面会有差异化展示；（3）合并逾期还款：仅支持惠民贷产品，惠民贷借款笔数大于1笔可使用此类还款方式。（4）单笔提前还款：即缩额还款，支持所有贷款品种；注意：如遇客户咨询房贷提前还款的所有相关事宜（含手机银行个人住房贷款提前还款功能，如：是否可以直接在手机银行申请房贷提前还款，申请后如何处理等），均请勿直接对外引导或指导，请按照《新一代客服受理流程—住房按揭贷款》进行处理。（5）合并提前还款：仅支持惠民贷产品，惠民贷借款笔数大于1笔可使用此类还款方式。\n个人手机银行还贷款-页面展示逻辑\n个人手机银行还贷款-页面展示逻辑:（1）用户同时有多贷款品种逾期或到期贷款，则分别展示逾期和到期品种；若同一个贷款品种同时存在到期和逾期状态，则需先展示逾期贷款，还清后再展示到期贷款；（2）用户只有到期或逾期贷款，则展示对应贷款信息；（3）用户名下还款未结清（状态正常），则展示近期待还信息。（4）用户名下无贷款记录或贷款已结清，则展示空页面。\n个人手机银行还贷款-操作步骤\n个人手机银行还贷款-操作步骤:（一）贷款首页的小图标，根据客群特色显示，如果没有在首页找到还贷款图标的，可在搜一搜中搜索“还贷款”进入。“还贷款”进入后，根据用户自身情况进入对应待还款信息页。（二）点击【立即还款】可进入对应贷款的到期、逾期还款流程；温馨提示：如客户转入资金后，如果由于贷款逾期被冻结，在尚未批量扣款前客户主动操作“逾期还款”，会优先使用冻结的资金。（三）点击【更多】可进入对应产品的贷后信息页面查看所有借款信息。",
                    "knowType": "1",
                    "createTime": "2026-07-20 19:20:28",
                    "updateTime": "2026-07-20 19:20:31",
                    "knowStatus": "已启用",
                    "fromAttachment": False
                }
            ],
            "param": None,
            "TRAN_ID": "",
            "TRANS_PROCESS": ""
        },
        "RSP_HEAD": {
            "TRAN_SUCCESS": "1",
            "TRACE_NO": "office-uat2-ellm-7586755dd-xfw2h-9199002134",
            "TRACE_ID": "0cf37c0d.1.65.4w0am7l4r5r",
            "PROCESS_STATUS_CODE": "N",
            "BIZ_TRACE_NO": None
        }
    }


def _vector_payload() -> dict:
    """行内搜索：照抄真实接口响应。"""
    return {
        "RSP_BODY": {
            "result": [
                {
                    "question": "科技星火贷",
                    "url": "http://12.244.151.131:8080/wiki_static/knowledge/H2gjOR2Pys/pKzJfZczuc/J4y9eKGLND/D6lwRjr5V2jZEy?type=ELLM&token=od96430ad84153cda9c19923a8997535ek20251215141156423",
                    "content": "交银人才贷（法人版）.html\n|在售实例编号|在售实例名称|在售实例简介|\n|-|-|-|\n|SL0000626|科创数据贷（园区）|该实例为分行特色定制，分行简介如下：厦门分行与厦门火炬管委会合作，针对园区内企业提供融资服务。|\n|SL0000072|科创智惠贷（无锡）|该实例为分行特色定制，分行简介如下：与无锡企业征信公司对接，针对两部委六清单客户提供线上信用融资。|\n|SL0000628|文科贷|文科贷为普惠科创场景定制产品。面向各类科技企园区孵化器小微企业，联合担保公司，基于数字技术、数据信息，采用“线上申请 线下核实”模式，通过系统规则、人工核实、担保公司评审相结合，对申请企业进行信贷评价，开发设计的线上流动资金贷款产品。|\n|SL0000629|科创数智贷（河北）|该实例为分行特色定制，分行简介如下：普惠科创场景定制产品。本产品聚焦两部委五大类科技客群（高新技术企业、“专精特新”中小企业、专精特新“小巨人”企业、国家技术创新示范企业、国家级制造业单项冠军），通过数字化手段，全面整合税务、征信、工商、1 N科技型企业评价模型、结算、创新积分等行内外数据，向科技型小微企业发放用于经营周转的线上信用贷款。",
                    "title": "交银人才贷（法人版）-2",
                    "score": "0.6334103",
                    "docGuid": "D6lwRjr5V2jZEy",
                    "docId": "FILE823969089571845",
                    "repository": "euvd-searchKnowledgeStandard",
                    "absContent": "交银人才贷（法人版）.html\n|在售实例编号|在售实例名称|在售实例简介|\n|-|-|-|\n|SL0000626|科创数据贷（园区）|该实例为分行特色定制，分行简介如下：厦门分行与厦门火炬管委会合作，针对园区内企业提供融资服务。|\n|SL0000072|科创智惠贷（无锡）|该实例为分行特色定制，分行简介如下：与无锡企业征信公司对接，针对两部委六清单客户提供线上信用融资。|\n|SL0000628|文科贷|文科贷为普惠科创场景定制产品。面向各类科技企园区孵化器小微企业，联合担保公司，基于数字技术、数据信息，采用“线上申请 线下核实”模式，通过系统规则、人工核实、担保公司评审相结合，对申请企业进行信贷评价，开发设计的线上流动资金贷款产品。|\n|SL0000629|科创数智贷（河北）|该实例为分行特色定制，分行简介如下：普惠科创场景定制产品。本产品聚焦两部委五大类科技客群（高新技术企业、“专精特新”中小企业、专精特新“小巨人”企业、国家技术创新示范企业、国家级制造业单项冠军），通过数字化手段，全面整合税务、征信、工商、1 N科技型企业评价模型、结算、创新积分等行内外数据，向科技型小微企业发放用于经营周转的线上信用贷款。",
                    "knowType": "2",
                    "createTime": "2026-07-06 09:20:11",
                    "updateTime": "2026-07-06 09:20:14",
                    "orgId": "1000027036",
                    "fullOrgName": "交通银行.普惠金融事业部/乡村振兴金融部",
                    "knowStatus": "已启用",
                    "fromAttachment": False
                },
                {
                    "question": "科技星火贷",
                    "url": "http://12.244.151.131:8080/wiki_static/knowledge/H2gjOR2Pys/pKzJfZczuc/J4y9eKGLND/-eqn9pA5ucyfpO?type=ELLM&token=od96430ad84153cda9c19923a8997535ek20251215141156423",
                    "content": "SJ-20260327-461-0005.html\n【概要描述】：科技企业维护任务中，加入“其他准入客群（火炬贷）”名单报错“用户已在科创名单中”，直接提交提示需加入一个科创名单。\n【详细描述】：\n【解决应用系统】：企业级用户信息管理系统",
                    "title": "SJ-20260327-461-0005-1",
                    "score": "0.15002882",
                    "docGuid": "-eqn9pA5ucyfpO",
                    "docId": "FILE826477977544965",
                    "repository": "euvd-searchKnowledgeStandard",
                    "absContent": "SJ-20260327-461-0005.html\n【概要描述】：科技企业维护任务中，加入“其他准入客群（火炬贷）”名单报错“用户已在科创名单中”，直接提交提示需加入一个科创名单。\n【详细描述】：\n【解决应用系统】：企业级用户信息管理系统",
                    "knowType": "2",
                    "createTime": "2026-07-13 11:31:39",
                    "updateTime": "2026-07-13 11:31:43",
                    "orgId": "5000001637",
                    "fullOrgName": "交通银行.数据中心（系统运营中心）.应用维护部",
                    "knowStatus": "已启用",
                    "fromAttachment": False
                }
            ],
            "param": None,
            "TRAN_ID": "",
            "TRANS_PROCESS": ""
        },
        "RSP_HEAD": {
            "TRAN_SUCCESS": "1",
            "TRACE_NO": "office-uat2-ellm-7586755dd-xfw2h-4268954310",
            "TRACE_ID": "0cf37c0d.1.49.4w0alz6tw57",
            "PROCESS_STATUS_CODE": "N",
            "BIZ_TRACE_NO": None
        }
    }


@app.post("/online")
async def mock_online(request: Request):
    body = await request.json()
    _log_request("online_search", body, headers=request.headers)
    return JSONResponse(_online_payload())


@app.post("/personal")
async def mock_personal(request: Request):
    form = await request.form()
    raw = form.get("REQ_MESSAGE")
    try:
        body = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        body = {}
    _log_request("personal_search", body, headers=request.headers)
    return JSONResponse(_personal_payload())


@app.post("/vector")
async def mock_vector(request: Request):
    body = await request.json()
    _log_request("vector_search", body, headers=request.headers)
    return JSONResponse(_vector_payload())


def _log_request(name: str, body, headers) -> None:
    """打印收到的请求，便于核对工具确实把 custom_params / 认证带到了后端。"""
    logger.info("---- %s 收到请求 ----", name)
    logger.info("auth headers: %s", dict(headers))
    logger.info("request body: %s", json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
