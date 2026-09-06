// ==UserScript==
// @name         Iwara 自动下载
// @namespace    http://tampermonkey.net/
// @version      0.2
// @description  自动获取视频信息并准备下载
// @author       你
// @match        https://www.iwara.tv/video/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @grant        unsafeWindow
// @connect      127.0.0.1
// ==/UserScript==

(function () {
    'use strict';

    const prepareApi = "http://127.0.0.1:6778/iwara/prepare_download";
    const logApi = "http://127.0.0.1:6778/iwara/log";

    const searchPattern2 = "_Source";

    let foundLink = null;

    let prepareFinished = false;


    // ===============================
    // 日志发送
    // ===============================
    function sendLog(level, message) {
        GM_xmlhttpRequest({
            method: "POST",
            url: logApi,
            headers: {
                "Content-Type": "application/json"
            },
            data: JSON.stringify({
                log: level,
                info: message
            }),
            onerror: function () {
                console.log("日志发送失败:", message);
            }
        });
    }


    // ===============================
    // 获取页面信息
    // ===============================
    function getPageInfo() {

        let titleElement = document.querySelector(".text--h1");

        let userElement = document.querySelector(
            ".page-video__byline__author .username"
        );


        if (!titleElement || !userElement) {
            return null;
        }


        let title = titleElement.textContent.trim();

        let userName = userElement.getAttribute("title");


        if (!title || !userName) {
            return null;
        }


        return {
            user_name: userName,
            title: title,
            page_url: window.location.href
        };
    }


    // ===============================
    // 请求准备下载
    // ===============================
    function prepareDownload(data) {

        sendLog(
            "INFO",
            "准备下载: " + JSON.stringify(data)
        );


        GM_xmlhttpRequest({
            method: "POST",
            url: prepareApi,
            headers: {
                "Content-Type": "application/json"
            },
            data: JSON.stringify(data),

            onload: function (response) {

                if (response.status === 200) {

                    sendLog(
                        "INFO",
                        "prepare_download 成功"
                    );

                    prepareFinished = true;

                    // 启动第二个定时器
                    startDownloadTimer();

                } else {

                    sendLog(
                        "ERROR",
                        "prepare_download 返回状态: " + response.status
                    );
                }
            },


            onerror: function () {

                sendLog(
                    "ERROR",
                    "prepare_download 请求失败"
                );

            }
        });
    }



    // ===============================
    // 第一个定时器
    // 查询用户名和标题
    // ===============================
    let infoTimer = setInterval(() => {

        let pageInfo = getPageInfo();


        if (pageInfo) {

            clearInterval(infoTimer);

            prepareDownload(pageInfo);

        } else {

            sendLog(
                "INFO",
                "等待页面信息..."
            );
        }


    }, 1000);



    // ===============================
    // 查找下载链接
    // ===============================
    function findLinks() {

        const links = document.querySelectorAll('a');


        links.forEach(link => {

            const href = link.href;


            if (href.includes(searchPattern2)) {

                foundLink = href;

            }

        });

    }



    // ===============================
    // 第二个定时器
    // 原下载逻辑
    // ===============================
    function startDownloadTimer() {


        let downloadTimer = setInterval(() => {


            findLinks();


            sendLog(
                "INFO",
                "查询下载链接中..."
            );


            if (foundLink) {


                clearInterval(downloadTimer);


                sendLog(
                    "INFO",
                    "找到下载链接: " + foundLink
                );


                const messageDiv = document.createElement('div');

                messageDiv.style.position = 'fixed';
                messageDiv.style.top = '50%';
                messageDiv.style.left = '50%';
                messageDiv.style.transform = 'translate(-50%, -50%)';
                messageDiv.style.zIndex = '9999';
                messageDiv.style.backgroundColor = 'rgba(0, 255, 0, 0.8)';
                messageDiv.style.padding = '20px';
                messageDiv.style.border = '1px solid #000';
                messageDiv.style.fontSize = '16px';
                messageDiv.style.fontWeight = 'bold';

                messageDiv.innerText =
                    '已找到下载链接，正在触发下载...';


                document.body.appendChild(messageDiv);


                window.location.href = foundLink;

            }


        }, 1000);

    }


})();