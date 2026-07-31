# #663 Live-Proof Marker

This documentation-only marker exists on disposable branches used to verify
that the Claude approval bridge binds evidence to a pull request, head branch,
and commit. The proof pull requests are closed without merging.

The second proof commit verifies that a code-changing push invalidates the
approval attached to the previous head while an earlier review is in flight.
