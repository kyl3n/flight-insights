// Injected by Rust only into this invocation's production main WebView.
(() => {
  const nonce = __Q22_NONCE_JSON__;
  const invoke = window.__TAURI_INTERNALS__?.invoke;
  if (!invoke) return; // Native deadline fails closed if IPC itself is absent.
  const evidence = {title:'',root_children:0,navigation_buttons:0,rendered_width:0,
    rendered_height:0,animation_frames:0,engine_connected:false,
    handshake:null,status:null,restart_handshake:null,error:null};
  const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
  (async () => {
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      const root = document.getElementById('root');
      const navigation = document.querySelector('nav[aria-label="Main navigation"]');
      if (document.readyState === 'complete' && root?.childElementCount > 0 && navigation &&
          document.body.innerText.includes('Engine connected')) {
        await frame(); await frame();
        const bounds = root.getBoundingClientRect();
        Object.assign(evidence,{title:document.title,root_children:root.childElementCount,
          navigation_buttons:navigation.querySelectorAll('button').length,
          rendered_width:bounds.width,rendered_height:bounds.height,
          animation_frames:2,engine_connected:true});
        evidence.handshake = await invoke('engine_request',{method:'handshake',params:{}});
        evidence.status = await invoke('engine_request',{method:'engine.status',params:{}});
        evidence.restart_handshake = await invoke('engine_request',{method:'engine.restart',params:{}});
        await invoke('native_gui_smoke_complete',{nonce,evidence});
        return;
      }
      await new Promise(resolve => setTimeout(resolve,100));
    }
    throw new Error('Production UI/engine connection did not become ready within 45 seconds.');
  })().catch(async () => {
    evidence.error = 'Production WebView initialization or IPC failed.';
    await invoke('native_gui_smoke_complete',{nonce,evidence}).catch(()=>{});
  });
})();
