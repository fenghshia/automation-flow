// ==UserScript==
// @name         Iwara 自动下载
// @namespace    http://tampermonkey.net/
// @version      0.1
// @description  在页面中查找包含指定字符串的链接并显示所有结果
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

    // 定义查找条件
    const searchPattern1 = "https://hanya.iwara.tv/download";
    const searchPattern2 = "_Source";

    let foundLink = null; // 用于存储匹配的链接

    // 函数：查找匹配的链接
    function findLinks() {
        const links = document.querySelectorAll('a'); // 获取页面所有的 <a> 标签
        links.forEach(link => {
            const href = link.href;
            // 如果链接包含两个指定的字符串
            if (href.includes(searchPattern2)) {
                foundLink = href; // 将第一个匹配的链接保存
            }
            // if (href.includes(searchPattern1) && href.includes(searchPattern2)) {
            // foundLink = href; // 将第一个匹配的链接保存
            // }
        });
    }

    // 定时器，每500毫秒检查一次页面
    let interval = setInterval(() => {
        findLinks(); // 查找匹配的链接
        console.log("查询链接中...");

        // 如果找到了匹配的链接，停止检查并执行下载
        if (foundLink) {
            clearInterval(interval); // 停止定时器

            // 在页面中显示提示信息
            const messageDiv = document.createElement('div');
            messageDiv.style.position = 'fixed';
            messageDiv.style.top = '50%';
            messageDiv.style.left = '50%';
            messageDiv.style.transform = 'translate(-50%, -50%)'; // 中心对齐
            messageDiv.style.zIndex = '9999';
            messageDiv.style.backgroundColor = 'rgba(0, 255, 0, 0.8)';
            messageDiv.style.padding = '20px';
            messageDiv.style.border = '1px solid #000';
            messageDiv.style.fontSize = '16px';
            messageDiv.style.fontWeight = 'bold';
            messageDiv.innerText = '已找到下载链接，正在触发下载...';
            document.body.appendChild(messageDiv);

            // 自动触发浏览器下载
            window.location.href = foundLink; // 跳转到下载链接，开始下载
        }
    }, 1000); // 每500毫秒检查一次
})();
