<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" expand-text="yes">
  <xsl:template match="/dataset[@version = '2']">
    <dataset><name>{title}</name></dataset>
  </xsl:template>
  <xsl:template match="/*" priority="-1">
    <xsl:message terminate="yes">Not a version 2 dataset</xsl:message>
  </xsl:template>
</xsl:stylesheet>
