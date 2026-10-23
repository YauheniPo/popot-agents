Copy owner/repo from the PR URL and verified metadata for the requested operation.
Read changed source with get_file_contents and sha set to head.sha, a commit SHA.
For fork source reads use head.repo.full_name; review operations target the PR repository.
The sha returned for a file identifies a blob, not a commit or tree.
Never use that blob SHA as the revision for get_file_contents or get_commit.
After an invalid revision error, recover the commit SHA from PR metadata before trying more file paths.
For directory listings request only name, path and type fields; paginate get_files rather than repeatedly reading a truncated full diff.
After submitting a review, verify publication once and return the result.
If any tool response is truncated, paginate or narrow the request; do not repeat an identical read hoping to receive different data.
Never repeat publication to compensate for a missing final answer.
