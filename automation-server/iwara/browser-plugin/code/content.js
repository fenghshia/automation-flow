document.addEventListener('click', (e) => {
  const link = e.target.closest('a');
  if (link && link.href && link.href.includes('/download?')) {
    e.preventDefault();
    e.stopPropagation();
    // Send message to background script to trigger the download request invisibly
    browser.runtime.sendMessage({ 
      action: "trigger_download", 
      url: link.href,
      page_url: window.location.href 
    });
  }
}, true);
