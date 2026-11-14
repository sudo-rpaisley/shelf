# Published navigation upgrade fixture

`67461926-app.css` is an unmodified export of the generated
`static/css/app.css` blob at commit
`67461926e29fa9494631407ae6182c6069e3bfd6`.

Keep it frozen: it models an already active Store service worker serving the
published bundle while upgraded HTML loads the independent navigation stylesheet.
In particular, its old clipped “Media” label rule must not be removed or weakened.
The browser test checks the fixture's SHA-256 before serving it.
