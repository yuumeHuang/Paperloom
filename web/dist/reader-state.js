import {aliasBlock} from './reading-core.js';

const defaults={percent:0,block:null,mode:'translated',bookmarks:[]};
const version=paper=>paper.manifest?.files?.find(file=>file.path==='document.json')?.sha256||paper.syncHash||null;
function normalize(document,value={}) {
  const valid=new Set(document.blocks.map(block=>block.id)),block=aliasBlock(document,value.block);
  return {...defaults,...value,block:valid.has(block)?block:null,bookmarks:[...new Set((Array.isArray(value.bookmarks)?value.bookmarks:[]).map(id=>aliasBlock(document,id)).filter(id=>valid.has(id)))]};
}

// A visible reader keeps its own position and document version. Cloud changes
// update the library; they never decide where the current reader should scroll.
export class ReadingState {
  constructor(saved){this.saved=saved;this.active=null;}
  open(paper) {
    const value=normalize(paper.document,this.saved[paper.id]);
    this.saved[paper.id]=value;this.active={id:paper.id,version:version(paper),value:structuredClone(value)};
    return this.active.value;
  }
  close(){this.active=null;}
  live(id){return this.active?.id===id?this.active.value:this.saved[id]||defaults;}
  isCurrent(paper){return !!paper&&this.active?.id===paper.id&&this.active.version===version(paper);}
  receive(paper,incoming) {
    let value=normalize(paper.document,{...this.saved[paper.id],...incoming});
    if(this.isCurrent(paper)) {
      this.active.value.bookmarks=[...value.bookmarks];
      const {percent,block,mode}=this.active.value;
      value={...value,percent,block,mode};
    }
    this.saved[paper.id]=value;
  }
  update(paper,latest,changes) {
    if(this.active?.id!==paper.id)return null;
    this.active.value={...this.active.value,...changes};
    if(!this.isCurrent(latest))return null;
    return this.saved[paper.id]=normalize(latest.document,this.active.value);
  }
  bookmark(paper,latest,id) {
    if(this.active?.id!==paper.id)return null;
    const list=this.active.value.bookmarks,value=!list.includes(id);
    this.active.value.bookmarks=value?[...list,id]:list.filter(block=>block!==id);
    const canonical=aliasBlock(latest.document,id);
    if(!latest.document.blocks.some(block=>block.id===canonical))return null;
    const saved=normalize(latest.document,this.saved[paper.id]);
    saved.bookmarks=value?[...new Set([...saved.bookmarks,canonical])]:saved.bookmarks.filter(block=>block!==canonical);
    this.saved[paper.id]=saved;return {block:canonical,value};
  }
}
