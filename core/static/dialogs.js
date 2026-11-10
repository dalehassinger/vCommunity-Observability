/* Shared confirm and alert dialogs for both containers (served by vco at /static/dialogs.js
   and by vco-ask at /shared/dialogs.js). They replace the browser's confirm() and alert()
   popups with a themed <dialog>, so they follow the light / dark theme.

     if (!(await vcoConfirm('Delete this chat?', { ok: 'Delete', danger: true }))) return;
     await vcoAlert('Could not delete the chats: ' + err.message);

   A form with data-confirm="Question?" asks before it submits (data-confirm-ok sets the
   button text). The dialog is modal (showModal), so focus stays inside it and Esc cancels.
   Clicking the backdrop also cancels: closedby="any" where the browser supports it, and a
   small click handler where it doesn't (Safari). No libraries; works at dark sites. */
(function () {
  'use strict';
  var dlg, text, okBtn, cancelBtn, settle = null;

  function build() {
    dlg = document.createElement('dialog');
    dlg.className = 'ui-dialog';
    dlg.setAttribute('closedby', 'any');
    dlg.setAttribute('aria-labelledby', 'ui-dialog-text');
    dlg.innerHTML = '<form method="dialog"><p id="ui-dialog-text" class="ui-dialog-text"></p>' +
      '<div class="ui-dialog-actions"><button type="submit" value="cancel" class="btn btn-outline" data-cancel>Cancel</button>' +
      '<button type="submit" value="ok" class="btn btn-primary" data-ok>OK</button></div></form>';
    text = dlg.querySelector('.ui-dialog-text');
    okBtn = dlg.querySelector('[data-ok]');
    cancelBtn = dlg.querySelector('[data-cancel]');
    // Esc, backdrop clicks and the buttons all end in "close"; returnValue says which.
    dlg.addEventListener('close', function () {
      var done = settle; settle = null;
      if (done) done(dlg.returnValue === 'ok');
    });
    if (!('closedBy' in HTMLDialogElement.prototype)) {
      dlg.addEventListener('click', function (e) {
        if (e.target !== dlg) return;
        var r = dlg.getBoundingClientRect();
        var inside = r.top <= e.clientY && e.clientY <= r.bottom && r.left <= e.clientX && e.clientX <= r.right;
        if (!inside) dlg.close('cancel');
      });
    }
    document.body.appendChild(dlg);
  }

  function open(message, opts, isAlert) {
    opts = opts || {};
    if (!dlg) build();
    if (settle) { settle(false); settle = null; }
    if (dlg.open) dlg.close('cancel');
    text.textContent = message;
    okBtn.textContent = opts.ok || 'OK';
    okBtn.classList.toggle('btn-danger', !!opts.danger);
    cancelBtn.hidden = !!isAlert;
    dlg.returnValue = '';
    return new Promise(function (resolve) {
      settle = resolve;
      dlg.showModal();
      (isAlert || !opts.danger ? okBtn : cancelBtn).focus();
    });
  }

  window.vcoConfirm = function (message, opts) { return open(message, opts, false); };
  window.vcoAlert = function (message, opts) { return open(message, opts, true).then(function () {}); };

  // <form data-confirm="..."> asks first, then submits for real.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.confirm || form.dataset.confirmed) return;
    e.preventDefault();
    var submitter = e.submitter;
    window.vcoConfirm(form.dataset.confirm, { ok: form.dataset.confirmOk || 'OK', danger: true }).then(function (yes) {
      if (!yes) return;
      form.dataset.confirmed = '1';
      form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
      delete form.dataset.confirmed;
    });
  }, true);
})();
