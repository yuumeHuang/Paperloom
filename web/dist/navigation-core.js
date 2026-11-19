// The same order is used for Android's system Back and the desktop Escape key.
export function backAction({dialogOpen,drawerOpen,findOpen,typeOpen,view}) {
  if(dialogOpen)return 'dialog';
  if(drawerOpen)return 'drawer';
  if(findOpen)return 'find';
  if(typeOpen)return 'type';
  if(view!=='library')return 'library';
  return 'background';
}
