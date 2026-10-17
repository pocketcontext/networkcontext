/// <reference path="../pb_data/types.d.ts" />
// Model hooks cover REST, batch and dashboard writes alike.
onRecordCreateExecute((e) => require(`${__hooks}/search.js`).sync(e), 'profiles', 'posts', 'messages');
onRecordUpdateExecute((e) => require(`${__hooks}/search.js`).sync(e), 'profiles', 'posts', 'messages');
