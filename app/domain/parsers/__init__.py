"""Domain parser modules for Blizzard data extraction"""

# Stamp written next to every parsed payload in storage. A stored row whose
# ``data_version`` differs from this value is re-parsed once on read and written
# back — that is what keeps a parser change effective immediately without
# putting a parse on every cache miss forever.
#
# Bump this whenever a parser's OUTPUT changes shape or content. Forgetting to
# bump means stored rows keep serving the old output until their own staleness
# threshold expires, which is the same failure the pre-parse design had.
#
# ponytail: one global int, so a change to any parser re-parses everything once.
# Split it into a per-category mapping only if that single re-parse pass ever
# shows up as a real cost — at this data volume it is a handful of parses.
PARSER_VERSION = 1
