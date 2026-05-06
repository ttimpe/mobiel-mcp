Please develop an MCP server that can be hosted in docker.

It should use the Westfalenfahrplan EFA API available at westfalenfahrplan.de to retrieve the following:
- Departure times
- Arrival times
- Stop searches
- Route searches
- Disruptions


The EFA API has several request types. There is an HAR file containing different types of requests in this directory.

Goal: It should be possible for claude on the web and locally to be asked questions about departure times, connections etc. and it should answer appropriately

