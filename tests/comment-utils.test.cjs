const {test} = require("node:test");
const assert = require("node:assert/strict");
require("../web/comment-utils.js");
const {timestamp, flatten, select} = globalThis.CommentUtils;

test("timestamps normalize seconds, milliseconds, ISO, missing and invalid times", () => {
  assert.equal(timestamp(1700000000), 1700000000000);
  assert.equal(timestamp("1700000000000"), 1700000000000);
  assert.equal(timestamp("2025-01-01T08:00:00+08:00"), timestamp("2025-01-01 00:00:00"));
  for (const value of [null, undefined, "", 0, "0", "invalid", "Infinity"]) assert.equal(timestamp(value), null);
});

const comments = Array.from({length:125}, (_, i) => ({
  comment_id:String(i), like_count:i, create_time:1700000000+i, sub_comments:[],
}));
comments[0].sub_comments = [
  {comment_id:"a", parent_comment_id:"0", like_count:1000, create_time:1700000200000, sub_comments:[
    {comment_id:"b", parent_comment_id:"a", like_count:999, create_time:"2025-01-01T00:00:00Z"},
  ]},
];

test("global top ten is selected before pagination and respects the comment scope", () => {
  assert.deepEqual(select(comments, "top10", "roots").map(c => c.comment_id),
    Array.from({length:10}, (_, i) => String(124-i)));
  assert.equal(select(comments, "top10", "all")[0].comment_id, "a");
  assert.equal(select(comments, "top10", "replies").length, 2);
  assert.equal(comments[0].comment_id, "0");
});

test("latest is numeric and flattened replies retain their root and direct target", () => {
  assert.equal(select(comments, "newest", "all")[0].comment_id, "b");
  const rows=flatten(comments);
  assert.equal(rows.length,127);
  assert.equal(rows[2].root_comment_id,"0");
  assert.equal(rows[2].parent_comment_id,"a");
});
