/* Thin fetch wrappers. The auth cookie is set by the launcher's first load. */

/* makeApi(setId) speaks for one set of documents: every call names it, so a
   step started in one set's tab works on that set whichever tab is showing
   by the time it finishes. window.api is the one for calls about no set. */
// A detail as words: a plain message as it is, a message inside an object,
// and FastAPI's list of validation errors one after another.
function describe(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(describe).filter(Boolean).join(" ");
  if (detail && typeof detail === "object") return describe(detail.message || detail.msg || "");
  return "";
}

window.makeApi = (setId) => {
  async function request(method, path, body) {
    const options = { method, headers: setId ? { "X-Set": setId } : {} };
    if (body instanceof FormData) {
      options.body = body;
    } else if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    if (!response.ok) {
      // the status and the server's own detail go with the message: a 404
      // from a route this build has not got is "not available", and a 422 on
      // the parties sheet names the field it is about
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (e) { /* keep */ }
      const error = new Error(describe(detail) || response.statusText);
      error.status = response.status;
      error.detail = detail;
      throw error;
    }
    return response.json();
  }

  const get = (path) => request("GET", path);
  const post = (path, body) => request("POST", path, body);
  const patch = (path, body) => request("PATCH", path, body);
  const put = (path, body) => request("PUT", path, body);
  const del = (path) => request("DELETE", path);

  /* Start a job-returning endpoint and poll it to completion. The pages
     read so far, of the pages the step will read, and the seconds since it
     started go to onProgress as a third argument. */
  async function job(start, onProgress) {
    const { job: id } = await start;
    const began = Date.now();
    for (;;) {
      const state = await get(`/api/jobs/${id}`);
      if (onProgress) onProgress(state.message, state.fraction, {
        done: state.pages_done || 0, total: state.pages_total || 0,
        seconds: (Date.now() - began) / 1000 });
      if (state.status === "done") return state.result;
      if (state.status === "error") throw new Error(state.error);
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
  }

  return { get, post, patch, put, del, job };
};

window.api = window.makeApi(null);
