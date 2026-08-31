<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * `addSql()` sem argumento nenhum. É `none` e não `unknown`: uma chamada sem SQL
 * não altera schema, e o parser sabe dizer isso. Sem esta linha, a única forma de
 * um `addSql()` vazio aparecer seria como um `unknown` que não é uma dúvida.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250201120500 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Migracao sem efeito, mantida para preservar a ordem das versoes';
    }

    public function up(Schema $schema): void
    {
        $this->addSql();
    }

    public function down(Schema $schema): void
    {
        $this->addSql();
    }
}
