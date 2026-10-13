// Injected by Rust only into this invocation's production main WebView.
(() => {
  const nonce = __Q22_NONCE_JSON__;
  const invoke = window.__TAURI_INTERNALS__?.invoke;
  if (!invoke) return; // Native deadline fails closed if IPC itself is absent.
  const evidence = {title:'',root_children:0,navigation_buttons:0,rendered_width:0,
    rendered_height:0,animation_frames:0,engine_connected:false,
    handshake:null,status:null,restart_handshake:null,phase:'waiting_for_ui',error:null};
  const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
  (async () => {
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      const root = document.getElementById('root');
      const navigation = document.querySelector('nav[aria-label="Main navigation"]');
      if (document.readyState === 'complete' && root?.childElementCount > 0 && navigation &&
          document.body.innerText.includes('Engine connected')) {
        evidence.phase = 'animation_frames';
        await frame(); await frame();
        const bounds = root.getBoundingClientRect();
        Object.assign(evidence,{title:document.title,root_children:root.childElementCount,
          navigation_buttons:navigation.querySelectorAll('button').length,
          rendered_width:bounds.width,rendered_height:bounds.height,
          animation_frames:2,engine_connected:true});
        evidence.phase = 'handshake';
        evidence.handshake = await invoke('engine_request',{method:'handshake',params:{}});
        evidence.phase = 'engine_status';
        evidence.status = await invoke('engine_request',{method:'engine.status',params:{}});
        evidence.phase = 'engine_restart';
        evidence.restart_handshake = await invoke('engine_request',{method:'engine.restart',params:{}});
        evidence.phase = 'completion';
        await invoke('native_gui_smoke_complete',{nonce,evidence});
        return;
      }
      await new Promise(resolve => setTimeout(resolve,100));
    }
    throw new Error('Production UI/engine connection did not become ready within 45 seconds.');
  })().catch(async error => {
    const message = typeof error?.message === 'string' ? error.message :
      (typeof error === 'string' ? error : 'Production WebView initialization or IPC failed.');
    const code = typeof error?.code === 'string' ? error.code.slice(0,64) + ': ' : '';
    evidence.error = (code + message).slice(0,1024);
    await invoke('native_gui_smoke_complete',{nonce,evidence}).catch(()=>{});
  });
})();
