import lmstudio as lms
import json
import logging
from env import EnvConfig
from .base import *


logger = logging.getLogger(__name__)


@app.route("/jdam/score", methods=["POST", "OPTIONS"])
def score():
    if request.method == "OPTIONS":
        return "", 200, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS"
        }
    data = request.get_json()
    logger.info("收到职位评分请求")
    myq = EnvConfig.score_rules()
    myq += """
>>>>>>>>>>以下是职位原始信息(冲突的信息均以描述中的为准, 冲突则扣分)
公司名称: {}
职位标题: {}
薪水: {}
工作年限: {}
学历要求: {}
工作地点: {}
描述: {}
""".format(data['companyName'], data['jobTitle'], data['salary'], data['workLimit'], data['degreeLimit'], data['location'], data['jdText'])
    jl = EnvConfig.resume()
    myq += """
>>>>>>>>>>以下是我的简历信息
{}
""".format(jl)
    # res = {"title": "职位标题", "company": "公司名称", "score": 60, "recommend": True, "reason": "推荐或不推荐原因", "summary": "职位关键信息摘要", "asr": "加减分数的过程"}
    interaction = gemini.models.generate_content(
        model="gemini-3.5-flash-low",
        contents=myq
    )
    res = json.loads(interaction.text)
    with open("res.json", "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=4)
    return jsonify(
        **res, isDuplicate=add_job_semantic_only(res["title"], res["company"], res["summary"])
    ), 200, {"Access-Control-Allow-Origin": "*"}
