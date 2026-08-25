/* Thin fetch wrappers. The auth cookie is set by the launcher's first load. */

window.api = (() => {
  async function request(method, path, body) {
    const options = { method, headers: {} };
    if (body instanceof FormData) {
      options.body = body;
    } else if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (e) { /* keep */ }
      throw new Error(detail);
    }
    return response.json();
  }

  const get = (path) => request("GET", path);
  const post = (path, body) => request("POST", path, body);
  const patch = (path, body) => request("PATCH", path, body);
  const del = (path) => request("DELETE", path);

  /* Start a job-returning endpoint and poll it to completion. */
  async function job(start, onProgress) {
    const { job: id } = await start;
    for (;;) {
      const state = await get(`/api/jobs/${id}`);
      if (onProgress) onProgress(state.message, state.fraction);
      if (state.status === "done") return state.result;
      if (state.status === "error") throw new Error(state.error);
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
  }

  return { get, post, patch, del, job };
})();
