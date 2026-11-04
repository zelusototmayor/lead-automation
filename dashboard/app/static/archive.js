(() => {
  "use strict";
  const append=(parent,tag,className,value)=>{const node=document.createElement(tag);node.className=className;node.textContent=String(value ?? "");parent.appendChild(node);return node;};
  document.addEventListener("DOMContentLoaded",()=>{
    const root=document.getElementById("archive-app");if(!root)return;
    const search=root.querySelector("[data-archive-search]"),list=root.querySelector("[data-archive-list]"),state=root.querySelector("[data-archive-state]");
    let offset=0,total=0,sequence=0,timer;
    const load=async()=>{
      const request=++sequence;state.textContent="A carregar o histórico…";state.classList.remove("hidden");
      const previous=root.querySelector("[data-archive-previous]"),next=root.querySelector("[data-archive-next]");previous.disabled=true;next.disabled=true;
      const params=new URLSearchParams({limit:"50",offset:String(offset)});const value=search.value.trim().slice(0,200);if(value)params.set("search",value);
      try {
        const response=await fetch(`/api/v1/archive?${params}`,{credentials:"same-origin",headers:{Accept:"application/json"}});
        if(!response.ok)throw Error("archive unavailable");const page=await response.json();if(request!==sequence)return;
        total=Number(page.total || 0);const items=Array.isArray(page.items)?page.items:[];list.replaceChildren();
        items.forEach(item=>{
          const row=append(list,"details","archive-row","");const summary=append(row,"summary","","");const heading=append(summary,"div","","");
          append(heading,"strong","",item.company || "Empresa não identificada");append(heading,"p","subtle",[item.contact,item.email].filter(Boolean).join(" · ") || "Sem contacto registado");
          append(summary,"span","archive-stage",item.stage || "Sem estado registado");append(summary,"span","archive-expand","Ver registo +");
          const body=append(row,"div","archive-detail","");
          append(body,"p","archive-origin",`Registo original${item.row_number != null ? ` · linha ${item.row_number}` : ""}`);
          if(item.reason)append(body,"p","subtle",String(item.reason).replaceAll("_"," "));
          const values=append(body,"dl","archive-values","");
          Object.entries(item.values || {}).forEach(([key,value])=>{
            if(value==null || value==="")return;
            append(values,"dt","",key);append(values,"dd","",typeof value === "object" ? JSON.stringify(value) : value);
          });
        });
        state.classList.toggle("hidden",items.length>0);state.textContent=value?"Sem resultados para esta pesquisa.":"Não há registos arquivados disponíveis.";
        root.querySelector("[data-archive-count]").textContent=`${total} ${total===1 ? `registo ${value?"encontrado":"preservado"}` : `registos ${value?"encontrados":"preservados"}`}`;
        root.querySelector("[data-archive-range]").textContent=total?`${offset+1}–${offset+items.length} de ${total}`:"0 registos";
        previous.disabled=offset===0;next.disabled=offset+50>=total;
      } catch(_) {if(request!==sequence)return;state.textContent="Não foi possível carregar o histórico. Tenta atualizar a página.";state.classList.remove("hidden");}
    };
    search.addEventListener("input",()=>{sequence++;window.clearTimeout(timer);offset=0;timer=window.setTimeout(load,250);});
    root.querySelector("[data-archive-previous]").addEventListener("click",()=>{offset=Math.max(0,offset-50);load();});
    root.querySelector("[data-archive-next]").addEventListener("click",()=>{if(offset+50<total){offset+=50;load();}});
    load();
  });
})();
