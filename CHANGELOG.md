# Changelog

All notable changes to this plugin will be documented in this file.

## [Unreleased]
- **Fixed**: Douyin API data path extraction (item under data, not jx)
- **Improved**: Error diagnostics with full API response logging
- **Added**: Support for both video and image content from Douyin
- **Added**: Douyin API integration from xingzhige.com
- **Removed**: Redundant logging in video parsing
- **Added**: Twitter/X links support and extended request timeouts to 180s
- **Implemented**: Fuzzy URL detection across all messages
- **Added**: Periodic cleanup task and manual /cleanup endpoint
- **Simplified**: Output to title + video component
- **Added**: Automatic link parsing without command prefix

## [1.0.1] - 2026-03-04
- Initial public release with TikTok, Douyin, YouTube, Vimeo, Instagram parsing
- Added cache cleanup
- Added DELETE /download endpoint
- Added README documentation
