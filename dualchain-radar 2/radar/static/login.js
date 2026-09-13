document.querySelector('#login-form').addEventListener('submit',async e=>{
  e.preventDefault(); const button=e.target.querySelector('button');button.disabled=true;
  const error=document.querySelector('#error');error.textContent='';
  try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.querySelector('#password').value})});
    const data=await r.json();if(!r.ok)throw Error(data.detail||'Sign in failed');location.href='/';
  }catch(err){error.textContent=err.message;}finally{button.disabled=false;}
});
