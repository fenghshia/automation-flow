const targetUrl = "http://127.0.0.1:6778/iwara/import_download";
const pendingDownloads = new Map();

browser.webRequest.onBeforeSendHeaders.addListener(
  function (details) {
    const url = details.url;
    const requestHeaders = details.requestHeaders;

    let pageUrl = details.originUrl || details.documentUrl;
    if (pendingDownloads.has(url)) {
      pageUrl = pendingDownloads.get(url);
      pendingDownloads.delete(url);
    }

    // Convert headers array to a simpler object
    const headersObj = {};
    if (requestHeaders) {
      for (let header of requestHeaders) {
        headersObj[header.name] = header.value;
      }
    }

    // Extract 'download' parameter for 'name'
    let downloadName = "";
    try {
      const urlObj = new URL(url);
      downloadName = urlObj.searchParams.get("download") || "";
    } catch (e) {
      console.error("Invalid URL:", e);
    }
    console.log(downloadName);

    // Send the URL and headers to the local server
    fetch(targetUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        // "page_url": details.originUrl || details.documentUrl,
        "page_url": pageUrl,
        "download_url": url,
        "headers": headersObj,
        "file_name": downloadName
      })
    }).catch(err => console.error("Error sending to local server:", err));

    // Cancel the original request to prevent the browser from downloading it directly
    // If you prefer to let the browser download it as well, return { cancel: false };
    if (details.tabId !== -1) {
      browser.tabs.remove(details.tabId).catch(err => console.error("Error closing tab:", err));
    }
    return { cancel: true };
  },
  { urls: ["*://*.iwara.tv/download?*"] },
  ["blocking", "requestHeaders"]
);

browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "trigger_download" && message.url) {
    pendingDownloads.set(message.url, message.page_url);
    // Perform fetch from background script.
    // This fetch will be intercepted by the webRequest listener above.
    fetch(message.url).catch(() => {
      // Ignore errors since webRequest will cancel the fetch
    });
    // Cleanup just in case it doesn't get intercepted
    setTimeout(() => pendingDownloads.delete(message.url), 10000);
  }
});
