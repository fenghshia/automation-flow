// ==UserScript==
// @name         AI 职位适配度分析看板 (API 拦截版)
// @namespace    http://tampermonkey.net/
// @version      5.0
// @description  通过拦截 detail.json 接口，获取最纯净的数据进行 AI 分析
// @match        *://*.zhipin.com/web/geek/jobs*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @grant        unsafeWindow
// @connect      127.0.0.1
// ==/UserScript==

(function() {
    'use strict';

    // ================= 配置区 =================
    const WEBHOOK_URL = "http://127.0.0.1:6778/jdam"; 
    const TARGET_API = "/wapi/zpgeek/job/detail.json"; // 我们要埋伏的接口

    // ================= 1. 核心技术：XHR 拦截器 =================
    // 必须在 document-start 阶段运行，赶在 Boss 直聘的 JS 加载前劫持原生 XHR
    const originalXHR = unsafeWindow.XMLHttpRequest;

    unsafeWindow.XMLHttpRequest = function() {
        const xhr = new originalXHR();
        
        // 监听请求完成事件
        xhr.addEventListener('load', function() {
            // 只要请求的 URL 包含目标 API，就立刻收网
            if (xhr.responseURL && xhr.responseURL.includes(TARGET_API)) {
                try {
                    const responseJson = JSON.parse(xhr.responseText);
                    
                    // 确保接口返回了有效数据
                    if (responseJson.code === 0 && responseJson.zpData && responseJson.zpData.jobInfo) {
                        console.log("🎯 成功拦截到纯净职位数据：", responseJson.zpData.jobInfo);
                        
                        // 从原始 JSON 中提取没有被字体加密的字段！
                        const jobInfo = responseJson.zpData.jobInfo;
                        const bossInfo = responseJson.zpData.bossInfo;
                        
                        const payload = {
                            jobTitle: jobInfo.jobName,
                            salary: jobInfo.salaryDesc,         // 纯净的薪资，绝对没有 \ue032 乱码
                            jdText: jobInfo.postDescription,    // 纯净的职位描述
                            companyName: bossInfo.brandName,
                            location: jobInfo.address, // 例如：成都武侯区
                            workLimit: jobInfo.experienceName,
                            degreeLimit: jobInfo.degreeName,
                            url: window.location.href
                        };

                        // 自动触发发送给你的 Flask 后端
                        sendToFlask(payload);
                    }
                } catch (e) {
                    console.error("解析 API 响应失败:", e);
                }
            }
        });
        return xhr;
    };

    // ================= 2. 注入 UI (复用你之前的完美设计) =================
    // 由于我们在 document-start 运行，此时 body 还没生成，需等待 DOMContentLoaded
    document.addEventListener('DOMContentLoaded', () => {
        GM_addStyle(`
            #jdam-helper-panel {
                position: fixed; right: 20px; top: 50%; transform: translateY(-50%);
                width: 320px; background: #ffffff; border-radius: 12px;
                box-shadow: 0 8px 24px rgba(0,0,0,0.12); border: 1px solid #ebeef5;
                padding: 16px; z-index: 999999;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
                transition: all 0.3s ease;
            }
            .jdam-section { margin-bottom: 12px; text-align: center; }
            .jdam-label { font-size: 12px; color: #909399; margin-bottom: 4px; }
            .jdam-value { font-size: 16px; font-weight: 600; color: #303133; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
            #jdam-score-val { font-size: 32px; color: #409EFF; font-weight: bold; }
            .score-high { color: #67C23A !important; } .score-mid { color: #E6A23C !important; } .score-low { color: #F56C6C !important; }
            #jdam-hint-box {
                margin-bottom: 12px; padding: 10px; background: #f8f9fa; border-radius: 6px;
                font-size: 13px; line-height: 1.6; text-align: left;
                word-wrap: break-word; white-space: pre-wrap; max-height: 150px;
                overflow-y: auto; display: none;
            }
            .text-green { color: #67C23A; } .text-red { color: #F56C6C; }
            #jdam-trigger-btn {
                width: 100%; padding: 10px 0; margin-top: 8px; background: #409EFF;
                color: white; border: none; border-radius: 6px; font-size: 14px;
                font-weight: bold; cursor: pointer; transition: background 0.2s;
            }
            #jdam-trigger-btn:hover { background: #66b1ff; }
            #jdam-trigger-btn:disabled { background: #c0c4cc !important; cursor: not-allowed; }
            .btn-duplicate { background: #F56C6C !important; } .btn-duplicate:hover { background: #f78989 !important; }
        `);

        const panelHTML = `
            <div id="jdam-helper-panel">
                <div class="jdam-section">
                    <div class="jdam-label">职位标题 (自动拦截)</div>
                    <div id="jdam-title-val" class="jdam-value" title="等待抓取...">等待抓取...</div>
                </div>
                <div class="jdam-section" style="border-top: 1px dashed #ebeef5; border-bottom: 1px dashed #ebeef5; padding: 12px 0;">
                    <div class="jdam-label">适配度打分</div>
                    <div id="jdam-score-val" class="jdam-value">0</div>
                </div>
                <div id="jdam-hint-box"></div>
                <button id="jdam-trigger-btn" disabled>等待接口通信...</button>
            </div>
        `;
        document.body.insertAdjacentHTML('beforeend', panelHTML);
    });

    // ================= 3. 与 Flask 后端通信的逻辑 =================
    function sendToFlask(payload) {
        const uiTitle = document.getElementById('jdam-title-val');
        const uiScore = document.getElementById('jdam-score-val');
        const uiBtn = document.getElementById('jdam-trigger-btn');
        const uiHintBox = document.getElementById('jdam-hint-box');

        // 更新 UI 状态为 Loading
        uiBtn.disabled = true;
        uiBtn.innerText = "AI 分析中...";
        uiBtn.classList.remove('btn-duplicate');
        uiTitle.innerText = payload.jobTitle;
        uiTitle.title = payload.jobTitle;
        uiScore.innerText = "-";
        uiScore.className = "jdam-value";
        uiHintBox.style.display = 'none';

        GM_xmlhttpRequest({
            method: "POST",
            url: WEBHOOK_URL + "/score",
            headers: { "Content-Type": "application/json" },
            data: JSON.stringify(payload),
            timeout: 30000,
            onload: function(res) {
                if (res.status >= 200 && res.status < 300) {
                    try {
                        const responseData = JSON.parse(res.responseText);
                        
                        // 恢复 UI 状态
                        uiBtn.disabled = false;
                        const score = parseInt(responseData.score) || 0;
                        uiScore.innerText = score;

                        if (score >= 80) uiScore.className = "jdam-value score-high";
                        else if (score >= 60) uiScore.className = "jdam-value score-mid";
                        else uiScore.className = "jdam-value score-low";

                        uiHintBox.style.display = 'block';
                        let hintContent = "";
                        let hintClass = "";

                        if (responseData.isDuplicate) {
                            uiBtn.innerText = "已判定重复";
                            uiBtn.classList.add('btn-duplicate');
                            hintClass = "text-red";
                            hintContent = "🚨 该职位已存在 (检测为重复发布)\n\n" + (responseData.reason || "");
                        } else {
                            uiBtn.innerText = "重新分析当前职位";
                            uiBtn.classList.remove('btn-duplicate');
                            
                            if (responseData.recommend === true) {
                                hintClass = "text-green";
                                hintContent = "✅ 推荐投递\n" + (responseData.reason || "");
                            } else if (responseData.recommend === false) {
                                hintClass = "text-red";
                                hintContent = "❌ 不推荐投递\n" + (responseData.reason || "");
                            } else {
                                hintClass = "";
                                hintContent = responseData.reason || "无额外提示信息";
                            }
                        }

                        uiHintBox.className = hintClass;
                        uiHintBox.innerText = hintContent;

                    } catch (e) {
                        uiBtn.innerText = "数据解析错误";
                    }
                } else {
                    uiBtn.innerText = "服务器响应错误";
                }
            },
            onerror: function() {
                uiBtn.innerText = "网络请求失败";
            }
        });
    }

})();