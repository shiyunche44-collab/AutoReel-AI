/* 脚本"有没有未保存的修改"的判断。
   判断错了会静默丢用户的编辑(关页面时靠它拦一下),所以单独抽出来测。 */

/**
 * 一批字段里改过的有几条。
 * @param {Array<{now: string, was: string}>} fields
 * @returns {number}
 */
export function countDirty(fields) {
  let n = 0;
  for (const f of fields) if (f.now !== f.was) n++;
  return n;
}

/**
 * 有没有未保存的修改。
 * @param {Array<{now: string, was: string}>} fields
 * @returns {boolean}
 */
export function isDirty(fields) {
  return fields.some((f) => f.now !== f.was);
}
