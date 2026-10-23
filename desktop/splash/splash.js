// Shown while the desktop shell starts the engine; the shell replaces this page when it answers.
setTimeout(function () {
  if (document.getElementById("failed").hidden) document.getElementById("slow").hidden = false;
}, 15000);

window.engineFailed = function (reason) {
  document.getElementById("status").hidden = true;
  document.getElementById("slow").hidden = true;
  document.getElementById("reason").textContent = reason;
  document.getElementById("failed").hidden = false;
};
