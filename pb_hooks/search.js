// Keep the FTS5 indexes in the same transaction as the record they describe.
const INDEXES = {
  profiles: ['profiles_search', ['name', 'headline', 'about']],
  posts: ['posts_search', ['text']],
  messages: ['messages_search', ['body']],
};

function sync(e) {
  e.next();
  const [table, columns] = INDEXES[e.record.collection().name];
  const db = e.app.db();
  db.newQuery(`DELETE FROM "${table}" WHERE record_id = {:id}`).bind({id: e.record.id}).execute();
  const params = {id: e.record.id};
  columns.forEach((column) => { params[column] = e.record.getString(column); });
  db.newQuery(`INSERT INTO "${table}" (record_id, ${columns.map((c) => '"' + c + '"').join(', ')}) VALUES ({:id}, ${columns.map((c) => '{:' + c + '}').join(', ')})`)
    .bind(params).execute();
}

module.exports = {sync};
