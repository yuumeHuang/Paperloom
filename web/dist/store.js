const DB = 'paperloom-library-v1';
let promise;
function database() {
  return promise ||= new Promise((resolve, reject) => {
    const request = indexedDB.open(DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore('papers', { keyPath: 'id' });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error('浏览器无法打开本地书库'));
  });
}
async function transaction(mode, action) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('papers', mode), request = action(tx.objectStore('papers'));
    let value;
    request.onsuccess = () => { value = request.result; };
    request.onerror = () => reject(new Error('本地书库操作失败'));
    tx.oncomplete = () => resolve(value);
    tx.onerror = () => reject(new Error('本地空间不足或存储权限被拒绝'));
    tx.onabort = () => reject(new Error('本地存储事务中断'));
  });
}
export const allPapers = () => transaction('readonly', store => store.getAll());
export const putPaper = paper => transaction('readwrite', store => store.put(paper));
export const removePaper = id => transaction('readwrite', store => store.delete(id));
