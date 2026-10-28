<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" expand-text="yes">
  <xsl:template match="/dataset">
    <record><title>{name}</title></record>
  </xsl:template>
  <xsl:template match="/*" priority="-1">
    <xsl:message terminate="yes">Not a dataset: {local-name()}</xsl:message>
  </xsl:template>
</xsl:stylesheet>
